"""Command-line interface for pillows-upload."""

# Copyright (c) 2026 edideaur
# SPDX-License-Identifier: MIT

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time
from collections.abc import Callable  # noqa: TC003
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Any

import niquests
from tqdm import tqdm

from .bench import _cmd_bench
from .config import Config
from .config_cli import _cmd_config
from .constants import (
    BASE_URL,
    DEFAULT_BACKOFF,
    DEFAULT_CHUNK_CONCURRENCY,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CONCURRENCY,
    DEFAULT_FORMAT,
    DEFAULT_OUTPUT_PATTERN,
    DEFAULT_PART_RETRIES,
    DEFAULT_RETRIES,
    DEFAULT_STATE_FILE,
    DEFAULT_TIMEOUT,
    ENV_API_KEY,
    ENV_IMGUR_KEY,
    IMGUR_BASE_URL,
    MB_DIVISOR,
    MIN_BACKOFF,
    MIN_CONCURRENCY,
    NONE_FORMAT,
    ZERO_RETRIES,
    __version__,
)
from .diagnostics import CircuitBreaker, run_doctor, verify_api_key
from .download import _cmd_download
from .imgur import _cmd_imgur_upload
from .logutils import JSON_LOG_ENV, configure_cli_logging, configure_logging
from .output import OutputWriter
from .remote import _cmd_ls, _cmd_rm
from .state import StateFile, _filter_already_uploaded, _get_uploaded_set
from .upload import _cfg_from_args, upload_one
from .utils import _resolve_default, collect_files, make_session
from .watch import _cmd_watch

logger = logging.getLogger(__name__)

COMPLETION_OPTIONS: list[str] = [
    "download",
    "imgur-upload",
    "config",
    "doctor",
    "bench",
    "watch",
    "ls",
    "rm",
    "-o",
    "-k",
    "--base-url",
    "--chunk-size",
    "-c",
    "--chunk-concurrency",
    "-r",
    "--part-retries",
    "--backoff",
    "--timeout",
    "--dry-run",
    "-v",
    "-q",
    "--version",
    "--resume",
    "--state-file",
    "--no-csv",
    "--delete",
    "--no-progress",
    "--completions",
    "--config",
    "--ext",
    "--min-size",
    "--max-size",
    "--format",
    "paths",
]


def _write_bash_completions() -> None:
    """Write bash shell completion script."""
    logger.info("COMPREPLY=()")
    opts = " ".join(COMPLETION_OPTIONS)
    logger.info('opts="%s"', opts)
    logger.info("_pillows_upload() {")
    logger.info('    local cur="${COMP_WORDS[COMP_CWORD]}"')
    logger.info('    COMPREPLY=( $(compgen -W "${opts}" -- "${cur}") )')
    logger.info("    return 0")
    logger.info("}")
    logger.info("complete -F _pillows_upload pillows-upload")


def _write_zsh_completions() -> None:
    """Write zsh shell completion script."""
    logger.info("#compdef pillows-upload")
    logger.info("_pillows_upload() {")
    logger.info("    local -a args")
    logger.info('    args=(" \\')
    for opt in COMPLETION_OPTIONS:
        logger.info("        '%s' \\", opt)
    logger.info('        "*:file:_files"')
    logger.info("    )")
    logger.info("    _arguments $args")
    logger.info("}")
    logger.info("(( ${+functions[compdef]} )) && compdef _pillows_upload pillows-upload")


def _write_fish_completions() -> None:
    """Write fish shell completion script."""
    logger.info("complete -c pillows-upload")
    for opt in COMPLETION_OPTIONS:
        if opt == "paths":
            logger.info("    -r")
        elif opt.startswith("--"):
            logger.info("    -l %s", opt[2:])
        elif opt.startswith("-"):
            logger.info("    -s %s", opt[1:])


COMPLETION_WRITERS: dict[str, Callable[[], None]] = {
    "bash": _write_bash_completions,
    "zsh": _write_zsh_completions,
    "fish": _write_fish_completions,
}


def print_completions(shell: str) -> None:
    """Print shell completion script for the given shell."""
    writer = COMPLETION_WRITERS.get(shell)
    if writer is None:
        msg = f"Unknown shell: {shell}"
        raise ValueError(msg)
    writer()


def _cmd_doctor(argv: list[str] | None = None) -> int:
    """Handle the ``doctor`` subcommand: run environment diagnostics."""
    p = argparse.ArgumentParser(
        prog="pillows-upload doctor",
        description="Check configuration, API keys, network, and HTTP/3.",
    )
    p.add_argument("-k", "--api-key", default=None, help="pillows API key (PILLOWS_KEY)")
    p.add_argument("--base-url", default=None, help=f"pillows API base URL (default: {BASE_URL})")
    p.add_argument("--imgur-key", default=None, help="imgur API key (IMGUR_KEY)")
    p.add_argument("--imgur-base-url", default=None, help=f"imgur API base URL (default: {IMGUR_BASE_URL})")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout (s)")
    args = p.parse_args(argv)

    api_key = args.api_key or os.environ.get(ENV_API_KEY)
    imgur_key = args.imgur_key or os.environ.get(ENV_IMGUR_KEY)
    return run_doctor(
        api_key=api_key,
        base_url=args.base_url or BASE_URL,
        imgur_key=imgur_key,
        imgur_base_url=args.imgur_base_url or IMGUR_BASE_URL,
        timeout=args.timeout,
    )


def _validate_positive(value: int | None, name: str) -> None:
    """Validate that a value is positive when provided."""
    if value is not None and value <= ZERO_RETRIES:
        msg = f"{name} must be greater than 0"
        raise ValueError(msg)


def _validate_non_negative(value: int | None, name: str) -> None:
    """Validate that a value is non-negative when provided."""
    if value is not None and value < ZERO_RETRIES:
        msg = f"{name} must be 0 or greater"
        raise ValueError(msg)


def validate_args(args: argparse.Namespace) -> None:
    """Validate parsed command-line arguments."""
    _validate_positive(args.chunk_size, "--chunk-size")
    if args.concurrency is not None and args.concurrency < MIN_CONCURRENCY:
        msg = "--concurrency must be at least 1"
        raise ValueError(msg)
    if args.chunk_concurrency is not None and args.chunk_concurrency < MIN_CONCURRENCY:
        msg = "--chunk-concurrency must be at least 1"
        raise ValueError(msg)
    _validate_non_negative(args.retries, "--retries")
    _validate_non_negative(args.part_retries, "--part-retries")
    if args.backoff is not None and args.backoff < MIN_BACKOFF:
        msg = "--backoff must be at least 1"
        raise ValueError(msg)
    _validate_positive(args.timeout, "--timeout")
    _validate_non_negative(args.min_size, "--min-size")
    _validate_non_negative(args.max_size, "--max-size")
    if (
        args.min_size is not None
        and args.max_size is not None
        and args.max_size > ZERO_RETRIES
        and args.max_size < args.min_size
    ):
        msg = "--max-size must be greater than or equal to --min-size"
        raise ValueError(msg)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    p = argparse.ArgumentParser(
        description="Upload files to pillows.su via chunked upload API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "paths",
        nargs="*",
        default=["./downloads"],
        help="files or directories to upload (default: ./downloads)",
    )
    p.add_argument(
        "-o",
        "--output",
        default=None,
        help="output path (default: upload_map.csv)",
    )
    p.add_argument(
        "-k",
        "--api-key",
        default=None,
        help="API key (default: PILLOWS_KEY env var)",
    )
    p.add_argument("--base-url", default=None, help=f"API base URL (default: {BASE_URL})")
    p.add_argument(
        "--chunk-size",
        type=int,
        default=None,
        help=f"chunk size in bytes (default: {DEFAULT_CHUNK_SIZE})",
    )
    p.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=None,
        help="parallel file uploads (default: 1)",
    )
    p.add_argument(
        "--chunk-concurrency",
        type=int,
        default=None,
        help="parallel chunk uploads per file (default: 1)",
    )
    p.add_argument(
        "-r",
        "--retries",
        type=int,
        default=None,
        help=f"retry count per file (default: {DEFAULT_RETRIES})",
    )
    p.add_argument(
        "--part-retries",
        type=int,
        default=None,
        help=f"retry count per chunk (default: {DEFAULT_PART_RETRIES})",
    )
    p.add_argument(
        "--backoff",
        type=int,
        default=None,
        help=f"exponential backoff base (default: {DEFAULT_BACKOFF})",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="simulate uploads without sending data",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="print detailed output")
    p.add_argument("-q", "--quiet", action="store_true", help="suppress all non-error output")
    p.add_argument(
        "--version",
        action="version",
        version=f"pillows-upload {__version__}",
        help="show version and exit",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="skip files already uploaded (requires state file)",
    )
    p.add_argument(
        "--state-file",
        default=None,
        help="state file for resume (default: .upload_state)",
    )
    p.add_argument("--no-csv", action="store_true", help="skip writing the output CSV")
    p.add_argument(
        "--delete",
        action="store_true",
        help="delete local files after successful upload",
    )
    p.add_argument("--no-progress", action="store_true", help="disable progress bars")
    p.add_argument(
        "--completions",
        choices=["bash", "zsh", "fish"],
        help="print shell completions",
    )
    p.add_argument(
        "--config",
        default=None,
        help="config file path (default: ~/.config/pillows-upload/config)",
    )
    p.add_argument(
        "--ext",
        nargs="*",
        help="only upload files with these extensions (e.g. .mp3 .wav)",
    )
    p.add_argument(
        "--min-size",
        type=int,
        default=None,
        help="skip files smaller than N bytes",
    )
    p.add_argument(
        "--max-size",
        type=int,
        default=None,
        help="skip files larger than N bytes (0 = no limit)",
    )
    p.add_argument(
        "--format",
        choices=["csv", "json", "ndjson", "html", "xlsx"],
        default=None,
        help="output format (default: csv)",
    )
    p.add_argument(
        "-T",
        "--turbo",
        action="store_true",
        help="max-throughput preset: concurrency=8 and chunk-concurrency=8",
    )
    p.add_argument(
        "--json-log",
        action="store_true",
        help="emit structured JSON log lines instead of plain text",
    )
    p.add_argument(
        "--no-circuit-breaker",
        action="store_true",
        help="disable the circuit breaker that pauses after repeated failures",
    )
    p.add_argument(
        "--adaptive",
        action="store_true",
        help="auto-tune concurrency up on success, down on errors",
    )
    return p.parse_args(argv)


def _resolve_args(args: argparse.Namespace, config: Config) -> None:
    """Resolve argument defaults from config and environment."""
    args.base_url = args.base_url or config.get("base_url") or BASE_URL
    args.api_key = args.api_key or os.environ.get(ENV_API_KEY) or config.get("api_key")
    args.chunk_size = _resolve_default(args.chunk_size, DEFAULT_CHUNK_SIZE)
    if args.turbo:
        args.concurrency = max(args.concurrency or DEFAULT_CONCURRENCY, 8)
        args.chunk_concurrency = max(args.chunk_concurrency or DEFAULT_CHUNK_CONCURRENCY, 8)
    else:
        args.concurrency = _resolve_default(args.concurrency, DEFAULT_CONCURRENCY)
        args.chunk_concurrency = _resolve_default(
            args.chunk_concurrency,
            DEFAULT_CHUNK_CONCURRENCY,
        )
    args.retries = _resolve_default(args.retries, DEFAULT_RETRIES)
    args.part_retries = _resolve_default(
        args.part_retries,
        DEFAULT_PART_RETRIES,
    )
    args.backoff = _resolve_default(args.backoff, DEFAULT_BACKOFF)
    args.timeout = _resolve_default(args.timeout, DEFAULT_TIMEOUT)
    args.min_size = _resolve_default(args.min_size, ZERO_RETRIES)
    args.max_size = _resolve_default(args.max_size, ZERO_RETRIES)
    if args.no_csv:
        args.format = NONE_FORMAT
    elif args.format is None and args.output is None:
        args.format = None
    elif args.format is None:
        args.format = DEFAULT_FORMAT
    args.state_file = _resolve_default(args.state_file, DEFAULT_STATE_FILE)
    if args.quiet:
        args.verbose = False


def _log_upload_result(result: dict[str, Any], *, verbose: bool) -> None:
    """Log upload result with optional verbose timing info."""
    if verbose:
        elapsed = result["elapsed"]
        mbps = (result["size"] / MB_DIVISOR) / elapsed if elapsed > 0 else ZERO_RETRIES
        logger.info(
            "  OK -> %s (%ss, %.2f MB/s, %d retries)",
            result["pillows_su_link"],
            elapsed,
            mbps,
            result["retries"],
        )
    else:
        logger.info("  OK -> %s", result["pillows_su_link"])


def _do_upload_main(  # noqa: PLR0913
    fpath: Path,
    session: niquests.Session | None,
    args: argparse.Namespace,
    *,
    state: StateFile | None,
    progress: bool,
    show_output: bool,
) -> dict[str, Any] | None:
    """Perform a single upload from the main CLI loop."""
    if show_output:
        logger.info("Uploading: %s", fpath.name)
    cfg = _cfg_from_args(args, session=session, state=state)
    cfg.progress = progress
    try:
        result = upload_one(fpath, cfg)
    except (RuntimeError, niquests.RequestException):
        logger.exception("  ERROR")
        return None
    else:
        if show_output:
            _log_upload_result(result, verbose=cfg.verbose)
        return result


def _write_output(
    results: list[dict[str, Any]],
    *,
    fmt: str | None,
    output_path: str | None,
    show_output: bool,
) -> None:
    """Write output file if format is specified."""
    if not fmt or fmt == NONE_FORMAT or not results:
        return
    output_path = output_path or DEFAULT_OUTPUT_PATTERN.format(ext=fmt)
    out = Path(output_path)
    writer = OutputWriter(fmt, str(out))
    with writer:
        for r in results:
            writer.write(r)
    if show_output:
        logger.info("\nOutput: %s", out.resolve())


def _delete_uploaded_files(
    results: list[dict[str, Any]],
    *,
    verbose: bool,
    show_output: bool,
) -> None:
    """Delete local files after successful upload."""
    for r in results:
        p = Path(r["file_path"])
        if p.exists():
            p.unlink()
            if show_output and verbose:
                logger.info("Deleted: %s", p)


def _print_summary(
    results: list[dict[str, Any]],
    *,
    errors: int,
    overall_start: float,
    show_output: bool,
) -> None:
    """Print upload summary statistics."""
    if not show_output:
        return
    overall_elapsed = time.time() - overall_start
    total_bytes = sum(r["size"] for r in results)
    avg_mbps = (total_bytes / MB_DIVISOR) / overall_elapsed if overall_elapsed > 0 else ZERO_RETRIES
    separator = "=" * 50
    logger.info("\n%s", separator)
    logger.info("Done. Uploaded: %d  Errors: %d", len(results), errors)
    logger.info(
        "Total: %.2f MB in %.2fs (%.2f MB/s)",
        total_bytes / MB_DIVISOR,
        overall_elapsed,
        avg_mbps,
    )
    logger.info("%s", separator)


def _validate_api_key(args: argparse.Namespace) -> int:
    """Validate API key is present. Returns exit code or 0."""
    if not args.api_key and not args.dry_run:
        logger.error(
            "Error: API key required. Set PILLOWS_KEY, use -k, or add it to config.",
        )
        return 1
    return ZERO_RETRIES


def _collect_and_filter_files(
    args: argparse.Namespace,
    state: StateFile | None,
) -> list[Path]:
    """Collect files and filter already uploaded ones."""
    files = collect_files(
        args.paths,
        extensions=args.ext,
        min_size=args.min_size,
        max_size=args.max_size,
        verbose=not args.quiet,
        state_file=getattr(args, "state_file", None),
    )
    if not files:
        return []
    uploaded = _get_uploaded_set(state)
    if args.resume and uploaded:
        before = len(files)
        files = _filter_already_uploaded(files, uploaded)
        skipped = before - len(files)
        if not args.quiet and skipped:
            logger.info("Resume: skipped %d already uploaded", skipped)
    return files


def _handle_keyboard_interrupt(
    results: list[dict[str, Any]],
    state: StateFile | None,
) -> int:
    """Save progress on interrupt. Returns exit code."""
    if state:
        for r in results:
            state.record(
                r["file_path"],
                size=r["size"],
                sha256=r["sha256"],
                parts_uploaded=r["parts_uploaded"],
                url=r["pillows_su_link"],
            )
    return 130


def _run_concurrent_uploads(  # noqa: C901, PLR0913
    files: list[Path],
    sessions: list[niquests.Session],
    args: argparse.Namespace,
    *,
    state: StateFile | None,
    progress: bool,
    show_output: bool,
    agg: tqdm | None,
    breaker: CircuitBreaker | None,
    writer: OutputWriter | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Run uploads concurrently and return (results, error_count)."""
    results: list[dict[str, Any]] = []
    errors = ZERO_RETRIES
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures: dict[Any, Path] = {}
        for i, fpath in enumerate(files):
            if breaker is not None and not breaker.allow():
                errors += 1
                continue
            session = sessions[i % len(sessions)]
            fut = pool.submit(
                _do_upload_main,
                fpath,
                session,
                args,
                state=state,
                progress=progress,
                show_output=show_output,
            )
            futures[fut] = fpath
        for future in as_completed(futures):
            try:
                result = future.result()
            except (RuntimeError, niquests.RequestException):  # noqa: PERF203
                errors += 1
                logger.exception("  ERROR")
                if breaker is not None:
                    breaker.record_failure()
            else:
                if result:
                    results.append(result)
                    if writer is not None:
                        writer.write(result)
                    if agg is not None:
                        agg.update(result["size"])
                    if breaker is not None:
                        breaker.record_success()
                else:
                    errors += 1
                    if breaker is not None:
                        breaker.record_failure()
    return results, errors


def _run_sequential_uploads(  # noqa: PLR0913
    files: list[Path],
    sessions: list[niquests.Session],
    args: argparse.Namespace,
    *,
    state: StateFile | None,
    progress: bool,
    show_output: bool,
    agg: tqdm | None,
    breaker: CircuitBreaker | None,
    writer: OutputWriter | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Run uploads sequentially and return (results, error_count)."""
    results: list[dict[str, Any]] = []
    errors = ZERO_RETRIES
    session = sessions[ZERO_RETRIES] if sessions else None
    for fpath in files:
        if breaker is not None and not breaker.allow():
            errors += 1
            continue
        result = _do_upload_main(
            fpath,
            session,
            args,
            state=state,
            progress=progress,
            show_output=show_output,
        )
        if result:
            results.append(result)
            if writer is not None:
                writer.write(result)
            if agg is not None:
                agg.update(result["size"])
            if breaker is not None:
                breaker.record_success()
        else:
            errors += 1
            if breaker is not None:
                breaker.record_failure()
    return results, errors


def _run_adaptive_uploads(  # noqa: C901, PLR0913
    files: list[Path],
    sessions: list[niquests.Session],
    args: argparse.Namespace,
    *,
    state: StateFile | None,
    progress: bool,
    show_output: bool,
    agg: tqdm | None,
    breaker: CircuitBreaker | None,
    writer: OutputWriter | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Upload with concurrency that ramps up on success and down on errors.

    A bounded pool of size ``max_concurrency`` is created, but only ``current``
    tasks are allowed in flight at once. Each success bumps ``current`` up, and
    each failure steps it down, so throughput self-tunes to the host.
    """
    results: list[dict[str, Any]] = []
    errors = ZERO_RETRIES
    min_c = 1
    max_c = max(args.concurrency * 4, 16)
    current = max(args.concurrency, 1)
    idx = ZERO_RETRIES
    in_flight = ZERO_RETRIES

    def submit_next() -> None:
        nonlocal errors, idx, in_flight
        while idx < len(files) and in_flight < current:
            if breaker is not None and not breaker.allow():
                errors += 1
                idx += 1
                continue
            fpath = files[idx]
            session = sessions[idx % len(sessions)]
            idx += 1
            in_flight += 1
            fut = pool.submit(
                _do_upload_main,
                fpath,
                session,
                args,
                state=state,
                progress=progress,
                show_output=show_output,
            )
            futures[fut] = fpath

    with ThreadPoolExecutor(max_workers=max_c) as pool:
        futures: dict[Any, Path] = {}
        submit_next()
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                in_flight -= 1
                futures.pop(future)
                try:
                    result = future.result()
                except (RuntimeError, niquests.RequestException):
                    errors += 1
                    logger.exception("  ERROR")
                    current = max(min_c, current - 1)
                    if breaker is not None:
                        breaker.record_failure()
                else:
                    if result:
                        results.append(result)
                        if writer is not None:
                            writer.write(result)
                        if agg is not None:
                            agg.update(result["size"])
                        current = min(max_c, current + 1)
                        if breaker is not None:
                            breaker.record_success()
                    else:
                        errors += 1
                        current = max(min_c, current - 1)
                        if breaker is not None:
                            breaker.record_failure()
            submit_next()
    return results, errors


def _run_upload_loop(  # noqa: PLR0913
    files: list[Path],
    sessions: list[niquests.Session],
    args: argparse.Namespace,
    *,
    state: StateFile | None,
    progress: bool,
    show_output: bool,
    writer: OutputWriter | None = None,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Run the upload loop. Returns (results, errors, interrupted)."""
    results: list[dict[str, Any]] = []
    errors = ZERO_RETRIES
    breaker = None if args.no_circuit_breaker else CircuitBreaker()
    total_bytes = sum(f.stat().st_size for f in files)
    agg = (
        tqdm(
            total=total_bytes,
            desc="Total",
            unit="B",
            unit_scale=True,
            unit_divisor=MB_DIVISOR,
            smoothing=0.1,
        )
        if progress
        else None
    )
    try:
        if args.adaptive and args.concurrency > MIN_CONCURRENCY:
            results, errors = _run_adaptive_uploads(
                files,
                sessions,
                args,
                state=state,
                progress=progress,
                show_output=show_output,
                agg=agg,
                breaker=breaker,
                writer=writer,
            )
        elif args.concurrency > MIN_CONCURRENCY:
            results, errors = _run_concurrent_uploads(
                files,
                sessions,
                args,
                state=state,
                progress=progress,
                show_output=show_output,
                agg=agg,
                breaker=breaker,
                writer=writer,
            )
        else:
            results, errors = _run_sequential_uploads(
                files,
                sessions,
                args,
                state=state,
                progress=progress,
                show_output=show_output,
                agg=agg,
                breaker=breaker,
                writer=writer,
            )
        interrupted = False
    except KeyboardInterrupt:
        if show_output:
            logger.info("\nInterrupted - saving progress...")
        interrupted = True
    finally:
        if agg is not None:
            agg.close()
    return results, errors, interrupted


def main(argv: list[str] | None = None) -> int:  # noqa: C901, PLR0911, PLR0912
    """Run the CLI entry point."""
    args_list = argv if argv is not None else sys.argv[1:]
    # Configure logging before dispatch so subcommands also get output.
    configure_logging(logging.INFO, json_log=os.environ.get(JSON_LOG_ENV) == "1")
    if args_list and args_list[0] == "download":
        return _cmd_download(args_list[1:])
    if args_list and args_list[0] == "imgur-upload":
        return _cmd_imgur_upload(args_list[1:])
    if args_list and args_list[0] == "config":
        return _cmd_config(args_list[1:])
    if args_list and args_list[0] == "doctor":
        return _cmd_doctor(args_list[1:])
    if args_list and args_list[0] == "bench":
        return _cmd_bench(args_list[1:])
    if args_list and args_list[0] == "watch":
        return _cmd_watch(args_list[1:])
    if args_list and args_list[0] == "ls":
        return _cmd_ls(args_list[1:])
    if args_list and args_list[0] == "rm":
        return _cmd_rm(args_list[1:])

    args = parse_args(argv)

    if args.completions:
        # Completions must be emitted as plain shell script, never JSON.
        configure_logging(logging.INFO, json_log=False)
        print_completions(args.completions)
        return ZERO_RETRIES

    configure_cli_logging(
        quiet=args.quiet,
        verbose=args.verbose,
        json_log=args.json_log or os.environ.get(JSON_LOG_ENV) == "1",
    )

    try:
        validate_args(args)
    except ValueError:
        logger.exception("Error")
        return 1

    config = Config(args.config)
    _resolve_args(args, config)

    exit_code = _validate_api_key(args)
    if exit_code:
        return exit_code

    if not args.dry_run:
        ok, msg = verify_api_key(args.base_url, args.api_key, timeout=args.timeout)
        if not ok:
            logger.error("Pre-flight API key check failed: %s", msg)
            return 1
        if not args.quiet:
            logger.info("Pre-flight: %s", msg)

    state = StateFile(Path(args.state_file)) if args.resume else None
    files = _collect_and_filter_files(args, state)
    if not files:
        if not args.quiet:
            logger.info("Error: no files found matching criteria")
        return 1

    show_output = not args.quiet
    if show_output:
        logger.info("Found %d file(s) to upload.", len(files))

    progress = not args.no_progress and not args.quiet
    sessions = [make_session() for _ in range(max(args.concurrency, MIN_CONCURRENCY))]
    overall_start = time.time()

    # Open the output writer up front and write each successful upload as it
    # finishes, so a crash/interrupt still leaves a complete CSV on disk.
    writer: OutputWriter | None = None
    if args.format and args.format != NONE_FORMAT:
        output_path = args.output or DEFAULT_OUTPUT_PATTERN.format(ext=args.format)
        writer = OutputWriter(args.format, str(output_path))

    cm: Any = writer if writer is not None else contextlib.nullcontext()
    with cm as w:
        results, errors, interrupted = _run_upload_loop(
            files,
            sessions,
            args,
            state=state,
            progress=progress,
            show_output=show_output,
            writer=w,
        )
    if interrupted:
        return _handle_keyboard_interrupt(results, state)

    for s in sessions:
        s.close()

    if writer is None:
        _write_output(
            results,
            fmt=args.format,
            output_path=args.output,
            show_output=show_output,
        )
    elif show_output:
        out_path = args.output or DEFAULT_OUTPUT_PATTERN.format(ext=args.format)
        logger.info("\nOutput: %s", Path(out_path).resolve())

    if args.delete:
        _delete_uploaded_files(
            results,
            verbose=args.verbose,
            show_output=show_output,
        )

    _print_summary(
        results,
        errors=errors,
        overall_start=overall_start,
        show_output=show_output,
    )
    return 1 if errors else ZERO_RETRIES
