"""Upload logic for pillows-upload."""

# Copyright (c) 2026 edideaur
# SPDX-License-Identifier: MIT

from __future__ import annotations

import argparse  # noqa: TC003
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

import niquests
from tqdm import tqdm

try:
    # niquests vendors urllib3_future; the traffic police raises this when a
    # pooled connection is torn down in another thread. It is a plain Exception
    # (not a niquests.RequestException), so it otherwise escapes every handler.
    from urllib3_future.util.traffic_police import UnavailableTraffic
except Exception:  # pragma: no cover - vendored path varies across niquests
    UnavailableTraffic = None  # type: ignore[assignment]

from .api import finalize_upload, init_upload, upload_part

# Exceptions that should be retried rather than aborting the upload. Includes
# UnavailableTraffic so a broken pooled connection in the concurrent path is
# retried instead of crashing the whole run.
_RETRIABLE_EXC: tuple[type[Exception], ...] = (niquests.RequestException, RuntimeError)
if UnavailableTraffic is not None:
    _RETRIABLE_EXC = _RETRIABLE_EXC + (UnavailableTraffic,)
from .constants import (
    BASE_URL,
    DEFAULT_BACKOFF,
    DEFAULT_CHUNK_CONCURRENCY,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_FORMAT,
    DEFAULT_PART_RETRIES,
    DEFAULT_RETRIES,
    DEFAULT_STATE_FILE,
    DEFAULT_TIMEOUT,
    DONE_TIMEOUT,
    DRY_RUN_PREFIX,
    ENV_API_KEY,
    KB_DIVISOR,
    MIN_CONCURRENCY,
    NONE_FORMAT,
    PILLOWS_URL_TEMPLATE,
    ZERO_RETRIES,
)
from .output import OutputWriter
from .state import StateFile, _filter_already_uploaded, _get_uploaded_set
from .utils import collect_files, compute_sha256, make_session

logger = logging.getLogger(__name__)


@dataclass
class UploadConfig:
    """Configuration for an upload operation."""

    base_url: str = BASE_URL
    api_key: str | None = None
    chunk_size: int = DEFAULT_CHUNK_SIZE
    retries: int = DEFAULT_RETRIES
    part_retries: int = DEFAULT_PART_RETRIES
    backoff: int = DEFAULT_BACKOFF
    timeout: int = DEFAULT_TIMEOUT
    dry_run: bool = False
    verbose: bool = False
    progress: bool = True
    session: niquests.Session | None = None
    state: StateFile | None = None
    chunk_concurrency: int = DEFAULT_CHUNK_CONCURRENCY


def _cfg_from_args(
    args: argparse.Namespace,
    *,
    session: niquests.Session | None = None,
    state: StateFile | None = None,
) -> UploadConfig:
    """Build an UploadConfig from argparse namespace."""
    return UploadConfig(
        base_url=args.base_url,
        api_key=args.api_key,
        chunk_size=getattr(args, "chunk_size", DEFAULT_CHUNK_SIZE),
        retries=getattr(args, "retries", DEFAULT_RETRIES),
        part_retries=getattr(args, "part_retries", DEFAULT_PART_RETRIES),
        backoff=getattr(args, "backoff", DEFAULT_BACKOFF),
        timeout=getattr(args, "timeout", DEFAULT_TIMEOUT),
        dry_run=args.dry_run,
        verbose=getattr(args, "verbose", False),
        progress=not getattr(args, "no_progress", False),
        session=session,
        state=state,
        chunk_concurrency=getattr(
            args,
            "chunk_concurrency",
            DEFAULT_CHUNK_CONCURRENCY,
        ),
    )


def _build_result(  # noqa: PLR0913
    *,
    abs_path: str,
    url: str,
    size: int,
    sha256: str,
    elapsed: float,
    parts_uploaded: int,
    total_retries: int,
) -> dict[str, Any]:
    """Build a result dict for a completed upload."""
    return {
        "file_path": abs_path,
        "pillows_su_link": url,
        "size": size,
        "sha256": sha256,
        "elapsed": elapsed,
        "parts_uploaded": parts_uploaded,
        "retries": total_retries,
    }


def _upload_chunk_with_retry(  # noqa: PLR0913
    session: niquests.Session,
    *,
    base_url: str,
    task_id: str,
    fname: str,
    chunk: bytes,
    part_no: int,
    api_key: str | None,
    timeout: int,
    part_retries: int,
    backoff: int,
    verbose: bool,
    pbar: tqdm | None,
) -> tuple[bool, int]:
    """Upload a single chunk with retries. Returns (success, retry_count)."""
    part_attempt = ZERO_RETRIES
    retries_used = ZERO_RETRIES
    while True:
        try:
            upload_part(
                session,
                base_url,
                task_id,
                fname,
                chunk,
                part_no,
                api_key=api_key,
                timeout=timeout,
            )
        except _RETRIABLE_EXC:  # noqa: PERF203
            part_attempt += 1
            retries_used += 1
            if part_attempt >= part_retries:
                return False, retries_used
            wait = backoff**part_attempt
            if verbose:
                logger.info(
                    "  Part %d retry %d/%d after %ds",
                    part_no,
                    part_attempt,
                    part_retries,
                    wait,
                )
            time.sleep(wait)
        else:
            if pbar is not None:
                pbar.update(len(chunk))
            return True, retries_used


def _upload_sequential(  # noqa: PLR0913
    session: niquests.Session,
    *,
    base_url: str,
    task_id: str,
    fname: str,
    chunk_iter: Iterator[tuple[int, bytes]],
    api_key: str | None,
    timeout: int,
    part_retries: int,
    backoff: int,
    verbose: bool,
    pbar: tqdm | None,
) -> tuple[int, int]:
    """Upload chunks sequentially. Returns (uploaded_parts, total_retries)."""
    uploaded = ZERO_RETRIES
    total_retries = ZERO_RETRIES
    for part_no, chunk in chunk_iter:
        ok, retries = _upload_chunk_with_retry(
            session,
            base_url=base_url,
            task_id=task_id,
            fname=fname,
            chunk=chunk,
            part_no=part_no,
            api_key=api_key,
            timeout=timeout,
            part_retries=part_retries,
            backoff=backoff,
            verbose=verbose,
            pbar=pbar,
        )
        if ok:
            uploaded += 1
        total_retries += retries
        if not ok:
            msg = f"part {part_no} failed after {part_retries} attempts"
            raise RuntimeError(msg)
    return uploaded, total_retries


def _upload_concurrent(  # noqa: PLR0913
    session: niquests.Session,
    *,
    base_url: str,
    task_id: str,
    fname: str,
    chunk_iter: Iterator[tuple[int, bytes]],
    api_key: str | None,
    timeout: int,
    part_retries: int,
    backoff: int,
    verbose: bool,
    pbar: tqdm | None,
    chunk_concurrency: int,
) -> tuple[int, int]:
    """Upload chunks concurrently with bounded in-flight memory.

    A semaphore caps how many chunks are read/buffered at once, so file data
    streams from disk into the network without materializing the whole file.
    """
    uploaded = ZERO_RETRIES
    total_retries = ZERO_RETRIES
    sem = threading.Semaphore(chunk_concurrency)
    with ThreadPoolExecutor(max_workers=chunk_concurrency) as pool:
        future_to_part: dict[Any, int] = {}
        for part_no, chunk in chunk_iter:
            sem.acquire()
            future = pool.submit(
                _upload_chunk_with_retry,
                session,
                base_url=base_url,
                task_id=task_id,
                fname=fname,
                chunk=chunk,
                part_no=part_no,
                api_key=api_key,
                timeout=timeout,
                part_retries=part_retries,
                backoff=backoff,
                verbose=verbose,
                pbar=pbar,
            )
            future_to_part[future] = part_no
            future.add_done_callback(lambda _f: sem.release())
        for future in as_completed(future_to_part):
            ok, retries = future.result()
            if ok:
                uploaded += 1
            total_retries += retries
            if not ok:
                msg = f"part {future_to_part[future]} failed after {part_retries} attempts"
                raise RuntimeError(msg)
    return uploaded, total_retries


def _iter_chunks(fpath: Path, chunk_size: int) -> Iterator[tuple[int, bytes]]:
    """Yield numbered chunks of a file without buffering it all in memory."""
    part_no = ZERO_RETRIES
    with fpath.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            part_no += 1
            yield part_no, chunk


def _is_cached_complete(
    cached: dict[str, Any] | None,
    *,
    size: int,
    sha256_hash: str,
    total_parts: int,
) -> bool:
    """Check if a cached entry represents a complete upload."""
    return bool(
        cached
        and cached.get("url")
        and cached.get("size") == size
        and cached.get("sha256") == sha256_hash
        and cached.get("parts_uploaded", ZERO_RETRIES) >= total_parts,
    )


def _attempt_upload(  # noqa: PLR0913
    fpath: Path,
    cfg: UploadConfig,
    *,
    abs_path: str,
    cached: dict[str, Any] | None,
    start: float,
    total_retries: int,
    sha256_hash: str,
) -> dict[str, Any]:
    """Attempt a single upload try. Raises on failure."""
    active_session = cfg.session or make_session()
    # Control-plane calls (init/finalize) use a dedicated session so they never
    # touch the connection pool that the concurrent chunk uploaders churn.
    # Reusing the shared session for finalize is what triggered
    # UnavailableTraffic ("a connection was broken, presumably in another
    # thread") and crashed the whole run.
    control_session = make_session()
    try:
        size = fpath.stat().st_size
        task_id = init_upload(
            control_session,
            cfg.base_url,
            fpath.name,
            size,
            api_key=cfg.api_key,
            timeout=cfg.timeout,
        )
        if cfg.verbose:
            logger.info("  Task ID: %s", task_id)

        skip_parts = ZERO_RETRIES
        if cached and cached.get("parts_uploaded"):
            skip_parts = cached["parts_uploaded"]
            if cfg.verbose:
                logger.info("  Resuming from part %d", skip_parts + 1)

        chunk_iter = _iter_chunks(fpath, cfg.chunk_size)
        if skip_parts:
            chunk_iter = (c for c in chunk_iter if c[0] > skip_parts)

        pbar: tqdm | None = None
        if cfg.progress and not cfg.verbose:
            pbar = tqdm(
                total=size,
                desc=fpath.name,
                unit="B",
                unit_scale=True,
                unit_divisor=KB_DIVISOR,
                leave=False,
                initial=skip_parts * cfg.chunk_size,
            )

        try:
            if cfg.chunk_concurrency > MIN_CONCURRENCY:
                uploaded_parts, chunk_retries = _upload_concurrent(
                    active_session,
                    base_url=cfg.base_url,
                    task_id=task_id,
                    fname=fpath.name,
                    chunk_iter=chunk_iter,
                    api_key=cfg.api_key,
                    timeout=cfg.timeout,
                    part_retries=cfg.part_retries,
                    backoff=cfg.backoff,
                    verbose=cfg.verbose,
                    pbar=pbar,
                    chunk_concurrency=cfg.chunk_concurrency,
                )
            else:
                uploaded_parts, chunk_retries = _upload_sequential(
                    active_session,
                    base_url=cfg.base_url,
                    task_id=task_id,
                    fname=fpath.name,
                    chunk_iter=chunk_iter,
                    api_key=cfg.api_key,
                    timeout=cfg.timeout,
                    part_retries=cfg.part_retries,
                    backoff=cfg.backoff,
                    verbose=cfg.verbose,
                    pbar=pbar,
                )
        finally:
            if pbar is not None:
                pbar.close()

        total_retries += chunk_retries

        # Finalize runs on the dedicated control session so it never touches
        # the connection pool the concurrent chunk uploaders churn.
        file_id = finalize_upload(
            control_session,
            cfg.base_url,
            task_id,
            api_key=cfg.api_key,
            timeout=DONE_TIMEOUT,
        )

        elapsed = time.time() - start
        url = PILLOWS_URL_TEMPLATE.format(file_id=file_id)

        if cfg.state:
            cfg.state.record(
                abs_path,
                size=size,
                sha256=sha256_hash,
                parts_uploaded=uploaded_parts,
                url=url,
            )

        return _build_result(
            abs_path=abs_path,
            url=url,
            size=size,
            sha256=sha256_hash,
            elapsed=round(elapsed, 2),
            parts_uploaded=uploaded_parts,
            total_retries=total_retries,
        )
    finally:
        control_session.close()


def upload_one(
    fpath: Path,
    cfg: UploadConfig,
) -> dict[str, Any]:
    """Upload a single file and return the result dict."""
    size = fpath.stat().st_size
    sha256_hash = compute_sha256(fpath)
    abs_path = str(fpath.resolve())
    total_parts = max(
        MIN_CONCURRENCY,
        (size + cfg.chunk_size - 1) // cfg.chunk_size,
    )

    if cfg.verbose:
        logger.info("  Size: %d bytes", size)

    if cfg.dry_run:
        return _build_result(
            abs_path=abs_path,
            url=PILLOWS_URL_TEMPLATE.format(
                file_id=DRY_RUN_PREFIX + fpath.name,
            ),
            size=size,
            sha256=sha256_hash,
            elapsed=0,
            parts_uploaded=ZERO_RETRIES,
            total_retries=ZERO_RETRIES,
        )

    cached = cfg.state.get(abs_path) if cfg.state else None
    if cached is not None and _is_cached_complete(
        cached,
        size=size,
        sha256_hash=sha256_hash,
        total_parts=total_parts,
    ):
        if cfg.verbose:
            logger.info(
                "  Skipping (unchanged): %s",
                cached.get("url", ""),
            )
        return _build_result(
            abs_path=abs_path,
            url=cached.get("url", ""),
            size=size,
            sha256=sha256_hash,
            elapsed=ZERO_RETRIES,
            parts_uploaded=cached.get("parts_uploaded", ZERO_RETRIES),
            total_retries=ZERO_RETRIES,
        )

    attempt = ZERO_RETRIES
    start = time.time()
    total_retries = ZERO_RETRIES

    while True:
        try:
            return _attempt_upload(
                fpath,
                cfg,
                abs_path=abs_path,
                cached=cached,
                start=start,
                total_retries=total_retries,
                sha256_hash=sha256_hash,
            )
        except _RETRIABLE_EXC as e:  # noqa: PERF203
            attempt += 1
            total_retries += 1
            if attempt >= cfg.retries:
                msg = f"Failed after {cfg.retries} attempts: {e}"
                raise RuntimeError(msg) from e
            wait = cfg.backoff**attempt
            if cfg.verbose:
                logger.info(
                    "  Retry %d/%d after %ds: %s",
                    attempt,
                    cfg.retries,
                    wait,
                    e,
                )
            time.sleep(wait)


def upload_task(
    fpath: Path,
    args: argparse.Namespace,
    _uploaded: set[str],
) -> dict[str, str] | None:
    """Upload a single file via argparse namespace. Returns result or None."""
    cfg = _cfg_from_args(
        args,
        session=getattr(args, "_session", None),
        state=getattr(args, "_state", None),
    )
    try:
        result = upload_one(fpath, cfg)
        return {
            "file_path": result["file_path"],
            "pillows_su_link": result["pillows_su_link"],
        }
    except (RuntimeError, niquests.RequestException):
        logger.exception("  ERROR")
        return None


def _run_lib_upload_loop(  # noqa: C901
    files: list[Path],
    sessions: list[niquests.Session],
    cfg: UploadConfig,
    *,
    state: StateFile | None,
    concurrency: int,
) -> list[dict[str, Any]]:
    """Run uploads for the library API. Returns results."""
    results: list[dict[str, Any]] = []

    def do_upload_lib(
        fpath: Path,
        session: niquests.Session | None,
    ) -> dict[str, Any] | None:
        """Upload a single file from the library API."""
        lib_cfg = replace(cfg, session=session, state=state)
        return upload_one(fpath, lib_cfg)

    try:
        if concurrency > MIN_CONCURRENCY:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(
                        do_upload_lib,
                        f,
                        sessions[i % len(sessions)],
                    ): f
                    for i, f in enumerate(files)
                }
                for future in as_completed(futures):
                    try:
                        result = future.result()
                    except (RuntimeError, niquests.RequestException):  # noqa: PERF203
                        pass
                    else:
                        if result:
                            results.append(result)
        else:
            session = sessions[ZERO_RETRIES] if sessions else None
            for fpath in files:
                try:
                    result = do_upload_lib(fpath, session)
                except (RuntimeError, niquests.RequestException):  # noqa: PERF203
                    pass
                else:
                    if result:
                        results.append(result)
    finally:
        for s in sessions:
            s.close()
    return results


def upload_files(  # noqa: PLR0913, C901
    paths: list[str],
    *,
    api_key: str | None = None,
    base_url: str = BASE_URL,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    concurrency: int = MIN_CONCURRENCY,
    chunk_concurrency: int = DEFAULT_CHUNK_CONCURRENCY,
    retries: int = DEFAULT_RETRIES,
    part_retries: int = DEFAULT_PART_RETRIES,
    backoff: int = DEFAULT_BACKOFF,
    timeout: int = DEFAULT_TIMEOUT,
    extensions: list[str] | None = None,
    min_size: int = ZERO_RETRIES,
    max_size: int = ZERO_RETRIES,
    dry_run: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    no_progress: bool = False,
    resume: bool = False,
    state_file: str = DEFAULT_STATE_FILE,
    delete: bool = False,
    output: str | None = None,
    output_format: str = DEFAULT_FORMAT,
) -> list[dict[str, Any]]:
    """Upload files programmatically and return results."""
    files = collect_files(
        paths,
        extensions=extensions,
        min_size=min_size,
        max_size=max_size,
        verbose=verbose,
    )
    if not files:
        return []

    state = StateFile(Path(state_file)) if resume else None
    uploaded = _get_uploaded_set(state)

    if resume and uploaded:
        files = _filter_already_uploaded(files, uploaded)

    if not files:
        return []

    resolved_key = api_key or os.environ.get(ENV_API_KEY)
    if not resolved_key and not dry_run:
        msg = "API key required. Pass api_key or set PILLOWS_KEY."
        raise ValueError(msg)

    sessions = [make_session() for _ in range(max(concurrency, MIN_CONCURRENCY))]
    progress_flag = not no_progress and not quiet

    cfg = UploadConfig(
        base_url=base_url,
        api_key=resolved_key,
        chunk_size=chunk_size,
        retries=retries,
        part_retries=part_retries,
        backoff=backoff,
        timeout=timeout,
        dry_run=dry_run,
        verbose=verbose,
        progress=progress_flag,
        chunk_concurrency=chunk_concurrency,
    )

    results = _run_lib_upload_loop(
        files,
        sessions,
        cfg,
        state=state,
        concurrency=concurrency,
    )

    if output and output_format != NONE_FORMAT and results:
        out = Path(output)
        writer = OutputWriter(output_format, str(out))
        with writer:
            for r in results:
                writer.write(r)

    if state:
        for r in results:
            state.record(
                r["file_path"],
                size=r["size"],
                sha256=r["sha256"],
                parts_uploaded=r.get("parts_uploaded", ZERO_RETRIES),
                url=r["pillows_su_link"],
            )

    if delete:
        for r in results:
            p = Path(r["file_path"])
            if p.exists():
                p.unlink()

    return results
