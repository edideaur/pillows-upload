"""Utility functions for pillows-upload."""

# Copyright (c) 2026 edideaur
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import niquests

from .constants import READ_CHUNK_SIZE, ZERO_RETRIES, T

if TYPE_CHECKING:
    from collections.abc import MutableMapping

logger = logging.getLogger(__name__)

# Shared across every session in the process so repeated connections to the
# same host skip the QUIC handshake (cached ALT-SVC / HTTP/3 capability).
_QUIC_CACHE: MutableMapping[Any, Any] = {}


def make_session(
    *,
    force_http3: bool = False,
    quic_cache: MutableMapping[Any, Any] | None = None,
) -> niquests.Session:
    """Create a pooled HTTP session optimized for throughput.

    niquests negotiates HTTP/3 (QUIC) by default; enabling ``multiplexed``
    lets many concurrent requests share a single connection as independent
    streams, which is the key throughput multiplier for chunked uploads.

    A process-wide QUIC cache is reused so subsequent connections to the same
    host avoid re-doing the handshake. When ``force_http3`` is set, HTTP/1 and
    HTTP/2 are disabled so the session only speaks HTTP/3 (useful once a host
    is known to support it). The negotiated protocol is reported once per host.
    """
    if not force_http3 and os.environ.get("PILLOWS_FORCE_HTTP3") == "1":
        force_http3 = True
    cache = quic_cache if quic_cache is not None else _QUIC_CACHE
    session = niquests.Session(
        multiplexed=True,
        disable_http1=force_http3,
        disable_http2=force_http3,
        quic_cache_layer=cache,
    )
    seen_protocols: set[str] = set()

    def _log_protocol(
        resp: niquests.PreparedRequest | niquests.Response,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        proto = getattr(resp, "http_version", None)
        if proto and proto not in seen_protocols:
            seen_protocols.add(proto)
            logger.info("Negotiated %s for %s", proto, getattr(resp, "url", ""))

    response_hooks = session.hooks.get("response")
    if isinstance(response_hooks, list):
        response_hooks.append(_log_protocol)
    return session


def _headers(api_key: str | None) -> dict[str, str]:
    """Build request headers with optional API key."""
    if api_key:
        return {"x-api-key": api_key}
    return {}


def _resolve_default(value: T | None, default: T) -> T:
    """Return value if not None, otherwise default."""
    if value is not None:
        return value
    return default


def compute_sha256(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(READ_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _should_skip_file(
    path: Path,
    *,
    ext_set: set[str] | None,
    min_sz: int,
    max_sz: int,
    verbose: bool,
) -> bool:
    """Check if a file should be skipped based on filters."""
    if not path.is_file():
        return True
    if ext_set and path.suffix.lower() not in ext_set:
        return True
    size = path.stat().st_size
    if size < min_sz:
        if verbose:
            logger.info("Skipping %s: too small (%d < %d)", path, size, min_sz)
        return True
    if max_sz > ZERO_RETRIES and size > max_sz:
        if verbose:
            logger.info("Skipping %s: too large (%d > %d)", path, size, max_sz)
        return True
    return False


def collect_files(
    paths: list[str],
    *,
    extensions: list[str] | None,
    min_size: int | None,
    max_size: int | None,
    verbose: bool,
) -> list[Path]:
    """Collect files matching the given filters from the provided paths."""
    ext_set = {e if e.startswith(".") else f".{e}" for e in extensions} if extensions else None
    min_sz = _resolve_default(min_size, 0)
    max_sz = _resolve_default(max_size, 0)
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = path.rglob("*")
        else:
            if verbose:
                logger.info("Skipping %s: not a file or directory", raw_path)
            continue
        files.extend(
            candidate
            for candidate in candidates
            if not _should_skip_file(
                candidate,
                ext_set=ext_set,
                min_sz=min_sz,
                max_sz=max_sz,
                verbose=verbose,
            )
        )
    return sorted(files)
