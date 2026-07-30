"""Imgur.gg upload functionality for pillows-upload."""

# Copyright (c) 2024 pillows-upload contributors
# SPDX-License-Identifier: MIT

from __future__ import annotations

import argparse
import logging
import mimetypes
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import niquests
from tqdm import tqdm

from .config import Config
from .constants import (
    DEFAULT_BACKOFF,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    ENV_IMGUR_KEY,
    IMGUR_BASE_URL,
    IMGUR_COMPLETE_ENDPOINT,
    IMGUR_DEFAULT_PART_SIZE,
    IMGUR_FILE_URL_TEMPLATE,
    IMGUR_MAX_FILES_PER_REQUEST,
    IMGUR_STATE_FILE,
    IMGUR_UPLOAD_ENDPOINT,
    KB_DIVISOR,
    MIN_CONCURRENCY,
    ZERO_RETRIES,
)
from .diagnostics import CircuitBreaker, verify_api_key
from .logutils import JSON_LOG_ENV, configure_cli_logging
from .output import OutputWriter
from .state import StateFile
from .utils import collect_files, compute_sha256, make_session

logger = logging.getLogger(__name__)


@dataclass
class ImgurConfig:
    """Configuration for an imgur.gg upload operation."""

    api_key: str | None = None
    base_url: str = IMGUR_BASE_URL
    retries: int = DEFAULT_RETRIES
    backoff: int = DEFAULT_BACKOFF
    timeout: int = DEFAULT_TIMEOUT
    dry_run: bool = False
    verbose: bool = False
    progress: bool = True
    concurrency: int = MIN_CONCURRENCY
    chunk_size: int = IMGUR_DEFAULT_PART_SIZE
    session: niquests.Session | None = None
    state: StateFile | None = None


def _imgur_headers(api_key: str | None) -> dict[str, str]:
    """Build imgur.gg request headers (uses x-api-key auth)."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _imgur_register(
    session: niquests.Session,
    cfg: ImgurConfig,
    metas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Register up to IMGUR_MAX_FILES_PER_REQUEST files in one request.

    ``metas`` is a list of ``{"fileName", "fileType", "fileSize"}`` dicts.
    Returns the ``files`` array from the API response, aligned by index.
    """
    resp = session.post(
        f"{cfg.base_url}{IMGUR_UPLOAD_ENDPOINT}",
        json={"files": metas},
        headers=_imgur_headers(cfg.api_key),
        timeout=cfg.timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        msg = f"imgur register failed: {payload.get('message')}"
        raise RuntimeError(msg)
    files = payload.get("files", [])
    if len(files) != len(metas):
        msg = "imgur register response count mismatch"
        raise RuntimeError(msg)
    return files


def _imgur_put_bytes(
    session: niquests.Session,
    cfg: ImgurConfig,
    url: str,
    data: bytes,
    content_type: str,
) -> None:
    """PUT raw file bytes to a single-file upload URL."""
    resp = session.put(
        url,
        data=data,
        headers={"Content-Type": content_type},
        timeout=cfg.timeout,
    )
    resp.raise_for_status()


def _imgur_put_part(
    session: niquests.Session,
    cfg: ImgurConfig,
    url: str,
    chunk: bytes,
) -> str:
    """PUT one multipart chunk and return its ETag header."""
    resp = session.put(url, data=chunk, timeout=cfg.timeout)
    resp.raise_for_status()
    return resp.headers.get("ETag", "")


def _imgur_finalize(  # noqa: PLR0913
    session: niquests.Session,
    cfg: ImgurConfig,
    file_id: str,
    nonce: str,
    *,
    upload_id: str | None = None,
    parts: list[dict[str, Any]] | None = None,
) -> None:
    """Finalize an uploaded file."""
    body: dict[str, Any] = {"fileId": file_id, "nonce": nonce}
    if upload_id is not None:
        body["uploadId"] = upload_id
    if parts is not None:
        body["parts"] = parts
    resp = session.put(
        f"{cfg.base_url}{IMGUR_COMPLETE_ENDPOINT}",
        json=body,
        headers=_imgur_headers(cfg.api_key),
        timeout=cfg.timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        msg = f"imgur finalize failed: {payload.get('message')}"
        raise RuntimeError(msg)


def _upload_one_registered(
    path: Path,
    cfg: ImgurConfig,
    reg: dict[str, Any],
    session: niquests.Session,
) -> dict[str, Any]:
    """Upload and finalize a single file given its registration entry."""
    file_id = reg["fileId"]
    nonce = reg["nonce"]
    is_multipart = bool(reg.get("isMultipart"))
    sha256_hash = compute_sha256(path)
    size = path.stat().st_size
    start = time.time()

    if not is_multipart:
        upload_url = reg.get("uploadUrl")
        if not upload_url:
            msg = f"imgur missing uploadUrl for {file_id}"
            raise RuntimeError(msg)
        content_type = reg.get("fileType") or "application/octet-stream"
        _imgur_put_bytes(session, cfg, upload_url, path.read_bytes(), content_type)
        _imgur_finalize(session, cfg, file_id, nonce)
    else:
        upload_id = reg.get("uploadId")
        part_urls = reg.get("partUrls") or []
        part_size = reg.get("partSize") or cfg.chunk_size
        if not upload_id or not part_urls:
            msg = f"imgur missing multipart info for {file_id}"
            raise RuntimeError(msg)
        parts: list[dict[str, Any]] = []
        with path.open("rb") as fh:
            part_no = ZERO_RETRIES
            for chunk in iter(lambda: fh.read(part_size), b""):
                if not chunk:
                    break
                part_no += 1
                url = part_urls[part_no - 1]["url"]
                etag = _imgur_put_part(session, cfg, url, chunk)
                parts.append({"PartNumber": part_no, "ETag": etag})
        _imgur_finalize(session, cfg, file_id, nonce, upload_id=upload_id, parts=parts)

    elapsed = time.time() - start
    result = {
        "file_path": str(path),
        "imgur_link": IMGUR_FILE_URL_TEMPLATE.format(file_id=file_id),
        "size": size,
        "sha256": sha256_hash,
        "elapsed": round(elapsed, 2),
        "retries": ZERO_RETRIES,
    }
    if cfg.state:
        cfg.state.record(
            str(path.resolve()),
            size=size,
            sha256=sha256_hash,
            parts_uploaded=1,
            url=result["imgur_link"],
        )
    return result


def _imgur_resume_filter(
    cfg: ImgurConfig,
    files: list[Path],
) -> tuple[list[Path], list[dict[str, Any]]]:
    """Split files into ones still to upload and already-completed cached ones.

    Returns ``(todo, done_results)``. ``done_results`` are reconstructed from
    the state file so resuming skips unchanged files without re-registering.
    """
    if not cfg.state:
        return list(files), []
    todo: list[Path] = []
    done: list[dict[str, Any]] = []
    for path in files:
        abs_path = str(path.resolve())
        cached = cfg.state.get(abs_path)
        if (
            cached
            and cached.get("url")
            and cached.get("size") == path.stat().st_size
            and cached.get("sha256") == compute_sha256(path)
        ):
            done.append(
                {
                    "file_path": abs_path,
                    "imgur_link": cached["url"],
                    "size": path.stat().st_size,
                    "sha256": cached["sha256"],
                    "elapsed": ZERO_RETRIES,
                    "retries": ZERO_RETRIES,
                }
            )
        else:
            todo.append(path)
    return todo, done


def imgur_upload_all(  # noqa: C901, PLR0912, PLR0915
    files: list[Path],
    cfg: ImgurConfig,
    breaker: CircuitBreaker | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Upload many files to imgur.gg using batch registration + concurrency.

    Returns ``(results, error_count)``.
    """
    results: list[dict[str, Any]] = []
    errors = ZERO_RETRIES

    todo, done = _imgur_resume_filter(cfg, files)
    if cfg.state and done and not cfg.verbose:
        logger.info("Resume: skipped %d already uploaded", len(done))

    total = len(todo)

    session = cfg.session or make_session()
    own_session = cfg.session is None

    pbar: tqdm | None = None
    if cfg.progress and not cfg.verbose:
        pbar = tqdm(
            total=total,
            desc="imgur",
            unit="file",
            unit_divisor=KB_DIVISOR,
            leave=False,
        )

    total_bytes = sum(f.stat().st_size for f in todo)
    agg: tqdm | None = None
    if cfg.progress and not cfg.verbose:
        agg = tqdm(
            total=total_bytes,
            desc="Total",
            unit="B",
            unit_scale=True,
            unit_divisor=KB_DIVISOR,
            smoothing=0.1,
        )

    try:
        q: queue.Queue[Any] = queue.Queue(maxsize=2)
        sentinel = object()

        def producer() -> None:
            i = ZERO_RETRIES
            while i < total:
                batch = todo[i : i + IMGUR_MAX_FILES_PER_REQUEST]
                i += len(batch)
                if cfg.dry_run:
                    q.put((batch, None))
                    continue
                metas = []
                for path in batch:
                    mime, _ = mimetypes.guess_type(str(path))
                    metas.append(
                        {
                            "fileName": path.name,
                            "fileType": mime or "application/octet-stream",
                            "fileSize": path.stat().st_size,
                        },
                    )
                try:
                    registered = _imgur_register(session, cfg, metas)
                except (RuntimeError, niquests.RequestException):
                    logger.exception("Batch registration failed")
                    q.put(sentinel)
                    return
                q.put((batch, registered))

        thread = threading.Thread(target=producer, daemon=True)
        thread.start()

        processed = ZERO_RETRIES
        while processed < total:
            item = q.get()
            if item is sentinel:
                errors += 1
                break
            batch, registered = item

            if cfg.dry_run:
                for path in batch:
                    size = path.stat().st_size
                    results.append(
                        {
                            "file_path": str(path),
                            "imgur_link": IMGUR_FILE_URL_TEMPLATE.format(
                                file_id=f"DRY_RUN_{path.name}",
                            ),
                            "size": size,
                            "sha256": compute_sha256(path),
                            "elapsed": 0,
                            "retries": ZERO_RETRIES,
                        }
                    )
                    if agg is not None:
                        agg.update(size)
                if pbar is not None:
                    pbar.update(len(batch))
                processed += len(batch)
                continue

            if cfg.concurrency > MIN_CONCURRENCY and len(batch) > MIN_CONCURRENCY:
                with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
                    futures = {
                        pool.submit(_upload_one_registered, path, cfg, reg, session): path
                        for path, reg in zip(batch, registered, strict=True)
                    }
                    for future in as_completed(futures):
                        try:
                            res = future.result()
                        except (RuntimeError, niquests.RequestException):  # noqa: PERF203
                            logger.exception("  ERROR")
                            errors += 1
                            if breaker is not None:
                                breaker.record_failure()
                        else:
                            results.append(res)
                            if agg is not None:
                                agg.update(res["size"])
                            if breaker is not None:
                                breaker.record_success()
                        finally:
                            if pbar is not None:
                                pbar.update(1)
            else:
                for path, reg in zip(batch, registered, strict=True):
                    if breaker is not None and not breaker.allow():
                        errors += 1
                        continue
                    try:
                        res = _upload_one_registered(path, cfg, reg, session)
                    except (RuntimeError, niquests.RequestException):
                        logger.exception("  ERROR")
                        errors += 1
                        if breaker is not None:
                            breaker.record_failure()
                    else:
                        results.append(res)
                        if agg is not None:
                            agg.update(res["size"])
                        if breaker is not None:
                            breaker.record_success()
                    finally:
                        if pbar is not None:
                            pbar.update(1)
            processed += len(batch)
        thread.join()
    finally:
        if pbar is not None:
            pbar.close()
        if agg is not None:
            agg.close()
        if own_session:
            session.close()

    results.extend(done)
    return results, errors


def _build_imgur_parser() -> argparse.ArgumentParser:
    """Build the argument parser for ``imgur-upload``."""
    p = argparse.ArgumentParser(
        prog="pillows-upload imgur-upload",
        description="Upload files to imgur.gg via the API.",
    )
    p.add_argument("paths", nargs="+", help="files or directories to upload")
    p.add_argument("-k", "--api-key", default=None, help="imgur API key (IMGUR_KEY)")
    p.add_argument("--base-url", default=None, help=f"API base URL (default: {IMGUR_BASE_URL})")
    p.add_argument("-c", "--concurrency", type=int, default=MIN_CONCURRENCY, help="parallel file uploads")
    p.add_argument("--chunk-size", type=int, default=IMGUR_DEFAULT_PART_SIZE, help="multipart part size")
    p.add_argument("-r", "--retries", type=int, default=DEFAULT_RETRIES, help="retry count")
    p.add_argument("--backoff", type=int, default=DEFAULT_BACKOFF, help="exponential backoff base")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout (s)")
    p.add_argument("-T", "--turbo", action="store_true", help="max-throughput preset (concurrency=8)")
    p.add_argument("-v", "--verbose", action="store_true", help="print detailed output")
    p.add_argument("-q", "--quiet", action="store_true", help="suppress all non-error output")
    p.add_argument("--dry-run", action="store_true", help="simulate uploads without sending data")
    p.add_argument("--no-progress", action="store_true", help="disable progress bars")
    p.add_argument("--ext", action="append", default=None, help="only files with these extensions")
    p.add_argument("--min-size", type=int, default=0, help="skip files smaller than N bytes")
    p.add_argument("--max-size", type=int, default=0, help="skip files larger than N bytes (0=no limit)")
    p.add_argument("-o", "--output", default=None, help="output path for results")
    p.add_argument("--format", default=None, help="output format: csv/json/ndjson/html/xlsx")
    p.add_argument("--config", default=None, help="config file path")
    p.add_argument(
        "--resume",
        action="store_true",
        help="skip files already uploaded (requires state file)",
    )
    p.add_argument(
        "--state-file",
        default=None,
        help=f"state file for resume (default: {IMGUR_STATE_FILE})",
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
    return p


def _cmd_imgur_upload(argv: list[str] | None = None) -> int:  # noqa: C901, PLR0912
    """Handle the imgur-upload subcommand: upload local files to imgur.gg."""
    args = _build_imgur_parser().parse_args(argv)

    configure_cli_logging(
        quiet=args.quiet,
        verbose=args.verbose,
        json_log=args.json_log or os.environ.get(JSON_LOG_ENV) == "1",
    )

    show_output = not args.quiet

    config = Config(args.config)
    api_key = args.api_key or os.environ.get(ENV_IMGUR_KEY) or config.get("imgur_api_key")
    if not api_key and not args.dry_run:
        logger.error(
            "Error: imgur API key required. Set IMGUR_KEY, use -k, or add it to config.",
        )
        return 1

    concurrency = max(args.concurrency, 8) if args.turbo else args.concurrency

    state_file = args.state_file or config.get("state_file") or IMGUR_STATE_FILE
    state = StateFile(Path(state_file)) if args.resume else None

    files = collect_files(
        args.paths,
        extensions=args.ext,
        min_size=args.min_size,
        max_size=args.max_size,
        verbose=args.verbose,
    )
    if not files:
        if show_output:
            logger.info("Error: no files found matching criteria")
        return 1

    if show_output:
        logger.info("Found %d file(s) to upload to imgur.gg.", len(files))

    session = make_session()
    cfg = ImgurConfig(
        api_key=api_key,
        base_url=args.base_url or IMGUR_BASE_URL,
        retries=args.retries,
        backoff=args.backoff,
        timeout=args.timeout,
        dry_run=args.dry_run,
        verbose=args.verbose,
        progress=not args.no_progress and show_output,
        concurrency=concurrency,
        chunk_size=args.chunk_size,
        session=session,
        state=state,
    )

    if not args.dry_run:
        ok, msg = verify_api_key(cfg.base_url, api_key, timeout=cfg.timeout)
        if not ok:
            logger.error("Pre-flight imgur API key check failed: %s", msg)
            return 1
        if show_output:
            logger.info("Pre-flight: %s", msg)

    breaker = None if args.no_circuit_breaker else CircuitBreaker()
    results, errors = imgur_upload_all(files, cfg, breaker=breaker)
    session.close()

    output = args.output
    fmt = args.format
    if output and fmt:
        writer = OutputWriter(fmt, output, link_key="imgur_link")
        with writer:
            for r in results:
                writer.write(r)

    for r in results:
        if show_output:
            logger.info("Uploaded: %s", r["imgur_link"])
        else:
            logger.info("%s", r["imgur_link"])

    if errors:
        return 1
    return ZERO_RETRIES
