"""Download functionality for pillows-upload."""

# Copyright (c) 2024 pillows-upload contributors
# SPDX-License-Identifier: MIT

from __future__ import annotations

import argparse
import logging
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

import niquests

from .config import Config
from .constants import (
    BASE_URL,
    CLONR_URL_PATTERN,
    DEFAULT_BACKOFF,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    ENV_API_KEY,
    IMGGUR_CDN_API,
    IMGGUR_FILE_URL_PATTERN,
    READ_CHUNK_SIZE,
    UPLOAD_TIMEOUT,
    ZERO_RETRIES,
)
from .logutils import JSON_LOG_ENV, configure_cli_logging
from .upload import UploadConfig, upload_one

logger = logging.getLogger(__name__)


def _http_get_json(url: str, *, retries: int, backoff: int, timeout: int) -> dict[str, Any]:
    """GET a URL and return parsed JSON, retrying on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = niquests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (niquests.RequestException, ValueError) as exc:  # noqa: PERF203
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(backoff**attempt)
                logger.warning("Retrying URL resolution (%d/%d)", attempt + 1, retries)
    msg = f"Failed to fetch {url}: {last_exc}"
    raise RuntimeError(msg) from last_exc


def _download_file(url: str, dest: Path, *, retries: int, backoff: int) -> None:
    """Download a file from url to dest, retrying on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = niquests.get(url, timeout=UPLOAD_TIMEOUT, stream=True)
            resp.raise_for_status()
        except niquests.RequestException as exc:  # noqa: PERF203
            last_exc = exc
            dest.unlink(missing_ok=True)
            if attempt < retries - 1:
                time.sleep(backoff**attempt)
                logger.warning("Retrying download (%d/%d)", attempt + 1, retries)
        else:
            with dest.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=READ_CHUNK_SIZE):
                    f.write(chunk)
            return
    msg = f"Download failed after {retries} attempts: {last_exc}"
    raise RuntimeError(msg) from last_exc


def _resolve_download_url(url: str) -> tuple[str, str]:
    """Resolve a download URL to (filename, download_url).

    Handles clonr.co, imgur.gg/f/, and generic URLs.
    """
    clonr_match = CLONR_URL_PATTERN.match(url)
    if clonr_match:
        encoded = clonr_match.group(1).split("?")[0]
        filename = urllib.parse.unquote(encoded)
        return filename, url

    imgur_match = IMGGUR_FILE_URL_PATTERN.match(url)
    if imgur_match:
        file_id = imgur_match.group(1)
        api_url = IMGGUR_CDN_API.format(file_id=file_id)
        data = _http_get_json(
            api_url,
            retries=DEFAULT_RETRIES,
            backoff=DEFAULT_BACKOFF,
            timeout=DEFAULT_TIMEOUT,
        )
        name = data.get("name", "")
        cdn_url = data.get("cdnUrl", "")
        if not name or not cdn_url:
            msg = f"Failed to parse file info for {file_id}"
            raise RuntimeError(msg)
        return name, cdn_url

    parsed = urllib.parse.urlparse(url)
    filename = Path(parsed.path).name
    filename = filename.split("?")[0] if filename else "download"
    if "imgur.gg" in url and "-" in filename:
        filename = filename.split("-", 1)[-1]
    return filename, url


def _download_then_upload(
    url: str,
    *,
    cfg: UploadConfig,
    show_output: bool,
) -> str | None:
    """Download ``url`` and upload it to pillows. Returns the link or None."""
    try:
        filename, download_url = _resolve_download_url(url)
    except (RuntimeError, niquests.RequestException):
        logger.exception("Failed to resolve URL: %s", url)
        return None

    if show_output:
        logger.info("Downloading: %s", filename)

    dest = Path(filename)
    try:
        _download_file(
            download_url,
            dest,
            retries=DEFAULT_RETRIES,
            backoff=DEFAULT_BACKOFF,
        )
    except (OSError, niquests.RequestException, RuntimeError):
        logger.exception("Download failed: %s", url)
        return None

    if not dest.exists():
        logger.error("Download failed: file not found after download")
        return None

    try:
        result = upload_one(dest, cfg)
    except (RuntimeError, niquests.RequestException):
        logger.exception("Upload failed: %s", url)
        return None
    finally:
        dest.unlink(missing_ok=True)

    return result["pillows_su_link"]


def _cmd_download(argv: list[str] | None = None) -> int:
    """Handle the download subcommand: download URLs and upload to pillows."""
    p = argparse.ArgumentParser(
        prog="pillows-upload download",
        description="Download a file from a URL and upload it to pillows.su.",
    )
    p.add_argument("url", nargs="*", help="URL(s) to download")
    p.add_argument("--list", default=None, help="file with one URL per line")
    p.add_argument("-k", "--api-key", default=None, help="API key (default: PILLOWS_KEY env var)")
    p.add_argument("--base-url", default=None, help=f"API base URL (default: {BASE_URL})")
    p.add_argument("-v", "--verbose", action="store_true", help="print detailed output")
    p.add_argument("-q", "--quiet", action="store_true", help="suppress all non-error output")
    p.add_argument("--dry-run", action="store_true", help="simulate upload without sending data")
    p.add_argument("--no-progress", action="store_true", help="disable progress bars")
    p.add_argument("--config", default=None, help="config file path")
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

    show_output = not args.quiet

    urls: list[str] = list(args.url)
    if args.list:
        with Path(args.list).open() as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    if not urls:
        logger.error("Error: no URLs provided (pass URLs or --list FILE)")
        return 1

    config = Config(args.config)
    api_key = args.api_key or os.environ.get(ENV_API_KEY) or config.get("api_key")
    if not api_key and not args.dry_run:
        logger.error(
            "Error: API key required. Set PILLOWS_KEY, use -k, or add it to config.",
        )
        return 1

    cfg = UploadConfig(
        base_url=args.base_url or config.get("base_url") or BASE_URL,
        api_key=api_key,
        dry_run=args.dry_run,
        verbose=args.verbose,
        progress=not args.no_progress and show_output,
    )

    ok = ZERO_RETRIES
    for url in urls:
        link = _download_then_upload(url, cfg=cfg, show_output=show_output)
        if link is None:
            ok += 1
            continue
        if show_output:
            logger.info("")
            logger.info("Done! Uploaded Link:")
            logger.info("%s", link)
        else:
            logger.info("%s", link)

    return 1 if ok else ZERO_RETRIES
