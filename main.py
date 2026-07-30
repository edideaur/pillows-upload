"""Bulk upload files to pillows.su via the chunked upload API."""

# Copyright (c) 2026 edideaur
# SPDX-License-Identifier: MIT

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import IO, TYPE_CHECKING, Any, Self, TypeVar

import requests
from tqdm import tqdm

if TYPE_CHECKING:
    import types

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

try:
    import openpyxl
except ImportError:
    openpyxl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

T = TypeVar("T")

BASE_URL = "https://api.pillows.su"
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 30
UPLOAD_TIMEOUT = 120
DONE_TIMEOUT = 300
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 2
DEFAULT_PART_RETRIES = 2
DEFAULT_CHUNK_CONCURRENCY = 1
DEFAULT_CONCURRENCY = 1
ZERO_RETRIES = 0
MIN_BACKOFF = 1
MIN_CONCURRENCY = 1
MIN_PART_RETRIES = 0
KB_DIVISOR = 1024
MB_DIVISOR = 1024 * 1024
READ_CHUNK_SIZE = 4 * 1024 * 1024
DRY_RUN_PREFIX = "DRY_RUN_"
PILLOWS_URL_TEMPLATE = "https://pillows.su/f/{file_id}"
ENV_API_KEY = "PILLOWS_KEY"
DEFAULT_STATE_FILE = ".upload_state"
DEFAULT_FORMAT = "csv"
DEFAULT_OUTPUT_PATTERN = "upload_map.{ext}"
NONE_FORMAT = "none"
CSV_EXT = "csv"
STATE_VERSION = 1
__version__ = "0.1.0"

CLONR_URL_PATTERN = re.compile(r"https?://clonr\.co/(.+)")
IMGGUR_FILE_URL_PATTERN = re.compile(r"https?://imgur\.gg/f/([a-zA-Z0-9]+)")
IMGGUR_CDN_API = "https://imgur.gg/api/file/{file_id}"


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
    session: requests.Session | None = None
    state: StateFile | None = None
    chunk_concurrency: int = DEFAULT_CHUNK_CONCURRENCY


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


def init_upload(  # noqa: PLR0913
    session: requests.Session | requests.api,
    base_url: str,
    fname: str,
    fsize: int,
    *,
    api_key: str | None,
    timeout: int,
) -> str:
    """Initialize a chunked upload and return the task ID."""
    resp = session.post(
        f"{base_url}/api/upload/init",
        json={"fileName": fname, "fileSize": fsize},
        headers=_headers(api_key),
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        msg = f"init failed: {payload.get('message')}"
        raise RuntimeError(msg)
    return payload["message"]["id"]


def upload_part(  # noqa: PLR0913, PLR0917
    session: requests.Session | requests.api,
    base_url: str,
    task_id: str,
    fname: str,
    chunk: bytes,
    part_no: int,
    *,
    api_key: str | None,
    timeout: int,
) -> None:
    """Upload a single chunk (part) of a file."""
    files = {"file": (fname, chunk, "application/octet-stream")}
    data = {"part": part_no}
    resp = session.put(
        f"{base_url}/api/upload/{task_id}/part",
        files=files,
        data=data,
        headers=_headers(api_key),
        timeout=timeout,
    )
    resp.raise_for_status()
    if not resp.json().get("success"):
        msg = f"part {part_no} failed: {resp.text}"
        raise RuntimeError(msg)


def finalize_upload(
    session: requests.Session | requests.api,
    base_url: str,
    task_id: str,
    *,
    api_key: str | None,
    timeout: int,
) -> str:
    """Finalize the upload and return the file ID."""
    resp = session.get(
        f"{base_url}/api/upload/{task_id}/done",
        headers=_headers(api_key),
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        msg = f"done failed: {payload.get('message')}"
        raise RuntimeError(msg)
    return payload["message"]["id"]


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


class StateFile:
    """Thread-safe JSONL state file for tracking upload progress."""

    def __init__(self, path: Path) -> None:
        """Initialize with the path to the state file."""
        self.path = path
        self.entries: dict[str, dict[str, Any]] = {}
        self.lock = Lock()
        self._load()

    def _load(self) -> None:
        """Load existing state from disk."""
        if not self.path.exists():
            return
        with self.path.open() as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                    self.entries[entry["path"]] = entry
                except (json.JSONDecodeError, KeyError):
                    self.entries[stripped] = {"path": stripped}

    def get(self, path: str) -> dict[str, Any] | None:
        """Return the state entry for a given path, or None."""
        return self.entries.get(path)

    def record(self, path: str, **kwargs: object) -> None:
        """Record upload metadata for a path and persist to disk."""
        with self.lock:
            self.entries[path] = {"path": path, **kwargs}
            self._write()

    def _write(self) -> None:
        """Atomically write all entries to disk."""
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w") as f:
            f.writelines(json.dumps(entry) + "\n" for entry in self.entries.values())
        tmp.replace(self.path)


class Config:
    """Load configuration from TOML or KEY=VALUE config files."""

    USER_CONFIG_DIR = Path.home() / ".config" / "pillows-upload"
    USER_CONFIG_FILE = USER_CONFIG_DIR / "config"

    def __init__(self, explicit_path: str | None = None) -> None:
        """Load config from explicit path or user config directory."""
        self.data: dict[str, str] = {}
        if explicit_path:
            self._load_file(Path(explicit_path))
        else:
            self._load_file(self.USER_CONFIG_FILE)

    def _load_file(self, path: Path) -> None:
        """Load a config file, auto-detecting format by extension."""
        if not path.is_file():
            return
        if path.suffix == ".toml":
            self._load_toml(path)
        else:
            self._load_env_file(path)

    def _load_env_file(self, path: Path) -> None:
        """Parse a KEY=VALUE file into key-value pairs."""
        with path.open() as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "=" not in stripped:
                    continue
                key, _, value = stripped.partition("=")
                self.data[key.strip()] = value.strip().strip('"').strip("'")

    def _load_toml(self, path: Path) -> None:
        """Parse a TOML config file into key-value pairs."""
        if tomllib is None:
            return
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
            self._extract_toml_sections(data)
        except (OSError, ValueError):
            return

    def _extract_toml_sections(self, data: dict[str, Any]) -> None:
        """Extract key-value pairs from supported TOML sections."""
        sections: list[dict[str, Any]] = []
        if "pillows-upload" in data:
            sections.append(data["pillows-upload"])
        tool_section = data.get("tool")
        if isinstance(tool_section, dict) and "pillows-upload" in tool_section:
            sections.append(tool_section["pillows-upload"])
        for section in sections:
            if isinstance(section, dict):
                for key, value in section.items():
                    if isinstance(value, str | int | float | bool):
                        self.data[key] = str(value)

    def get(self, key: str, default: str | None = None) -> str | None:
        """Return a config value by key, or default if not found."""
        return self.data.get(key, default)


class OutputWriter:
    """Context manager for writing upload results to various formats."""

    def __init__(self, fmt: str, path: str) -> None:
        """Initialize with output format and file path."""
        self.fmt = fmt
        self.path = path
        self._file: IO[str] | None = None
        self._writer: csv.DictWriter[str] | None = None
        self._results: list[dict[str, Any]] = []

    def __enter__(self) -> Self:
        """Open file handles for streaming formats."""
        if self.fmt in (CSV_EXT, "ndjson"):
            self._file = Path(self.path).open("w", newline="")
            if self.fmt == CSV_EXT:
                self._writer = csv.DictWriter(
                    self._file,
                    fieldnames=["file_path", "pillows_su_link"],
                )
                self._writer.writeheader()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        """Close file handles and flush buffered formats."""
        if self._file:
            self._file.close()
        if exc_type is None and self.fmt not in (CSV_EXT, "ndjson"):
            self._flush_buffered()

    def write(self, result: dict[str, Any]) -> None:
        """Write a single upload result."""
        if self.fmt == CSV_EXT and self._writer is not None and self._file is not None:
            self._writer.writerow(
                {"file_path": result["file_path"], "pillows_su_link": result["pillows_su_link"]},
            )
            self._file.flush()
        elif self.fmt == "ndjson" and self._file is not None:
            self._file.write(json.dumps(result) + "\n")
            self._file.flush()
        else:
            if self.fmt == "xlsx" and openpyxl is None:
                msg = "openpyxl is required for xlsx output. Install with: pip install openpyxl"
                raise RuntimeError(msg)
            self._results.append(result)

    def _flush_buffered(self) -> None:
        """Write buffered results to disk for json/html/xlsx formats."""
        if self.fmt == "json":
            self._write_json()
        elif self.fmt == "html":
            self._write_html()
        elif self.fmt == "xlsx":
            self._write_xlsx()

    def _write_json(self) -> None:
        """Write results as JSON array."""
        with Path(self.path).open("w") as f:
            json.dump(self._results, f, indent=2)

    def _write_html(self) -> None:
        """Write results as HTML table."""
        with Path(self.path).open("w") as f:
            f.write(
                "<html><body><table><tr><th>file_path</th><th>pillows_su_link</th></tr>",
            )
            for r in self._results:
                link = r["pillows_su_link"]
                f.write(
                    f"<tr><td>{r['file_path']}</td><td><a href='{link}'>{link}</a></td></tr>",
                )
            f.write("</table></body></html>")

    def _write_xlsx(self) -> None:
        """Write results as XLSX spreadsheet."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["file_path", "pillows_su_link"])
        for r in self._results:
            ws.append([r["file_path"], r["pillows_su_link"]])
        wb.save(self.path)


COMPLETION_OPTIONS: list[str] = [
    "download",
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


COMPLETION_WRITERS: dict[str, callable[[], None]] = {
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
        resp = requests.get(api_url, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
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


def _download_file(url: str, dest: Path) -> None:
    """Download a file from url to dest."""
    resp = requests.get(url, timeout=UPLOAD_TIMEOUT, stream=True)
    resp.raise_for_status()
    with dest.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=READ_CHUNK_SIZE):
            f.write(chunk)


def _cmd_download(argv: list[str] | None = None) -> int:
    """Handle the download subcommand: download a URL and upload to pillows."""
    p = argparse.ArgumentParser(
        prog="pillows-upload download",
        description="Download a file from a URL and upload it to pillows.su.",
    )
    p.add_argument("url", help="URL to download")
    p.add_argument("-k", "--api-key", default=None, help="API key (default: PILLOWS_KEY env var)")
    p.add_argument("--base-url", default=None, help=f"API base URL (default: {BASE_URL})")
    p.add_argument("-v", "--verbose", action="store_true", help="print detailed output")
    p.add_argument("-q", "--quiet", action="store_true", help="suppress all non-error output")
    p.add_argument("--dry-run", action="store_true", help="simulate upload without sending data")
    p.add_argument("--no-progress", action="store_true", help="disable progress bars")
    p.add_argument("--config", default=None, help="config file path")
    args = p.parse_args(argv)

    show_output = not args.quiet

    try:
        filename, download_url = _resolve_download_url(args.url)
    except (RuntimeError, requests.RequestException):
        logger.exception("Failed to resolve URL")
        return 1

    if show_output:
        logger.info("Downloading: %s", filename)

    dest = Path(filename)
    try:
        _download_file(download_url, dest)
    except (OSError, requests.RequestException):
        logger.exception("Download failed")
        return 1

    if not dest.exists():
        logger.error("Download failed: file not found after download")
        return 1

    config = Config(args.config)
    api_key = args.api_key or os.environ.get(ENV_API_KEY) or config.get("api_key")
    if not api_key and not args.dry_run:
        logger.error(
            "Error: API key required. Set PILLOWS_KEY, use -k, or add it to config.",
        )
        return 1

    if show_output:
        logger.info("Uploading to pillows...")

    cfg = UploadConfig(
        base_url=args.base_url or config.get("base_url") or BASE_URL,
        api_key=api_key,
        dry_run=args.dry_run,
        verbose=args.verbose,
        progress=not args.no_progress and show_output,
    )

    try:
        result = upload_one(dest, cfg)
    except (RuntimeError, requests.RequestException):
        logger.exception("Upload failed")
        return 1

    link = result["pillows_su_link"]
    dest.unlink(missing_ok=True)

    if show_output:
        logger.info("")
        logger.info("Done! Uploaded Link:")
        logger.info("%s", link)
    else:
        logger.info("%s", link)

    return ZERO_RETRIES


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
        version=f"pillows-uploader {__version__}",
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
    return p.parse_args(argv)


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
            candidates = sorted(path.rglob("*"))
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
    session: requests.Session | requests.api,
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
        except (requests.RequestException, RuntimeError):  # noqa: PERF203
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
    session: requests.Session | requests.api,
    *,
    base_url: str,
    task_id: str,
    fname: str,
    chunks: list[tuple[int, bytes]],
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
    for part_no, chunk in chunks:
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
    session: requests.Session | requests.api,
    *,
    base_url: str,
    task_id: str,
    fname: str,
    chunks: list[tuple[int, bytes]],
    api_key: str | None,
    timeout: int,
    part_retries: int,
    backoff: int,
    verbose: bool,
    pbar: tqdm | None,
    chunk_concurrency: int,
) -> tuple[int, int]:
    """Upload chunks concurrently. Returns (uploaded_parts, total_retries)."""
    uploaded = ZERO_RETRIES
    total_retries = ZERO_RETRIES
    with ThreadPoolExecutor(max_workers=chunk_concurrency) as pool:
        future_to_part = {
            pool.submit(
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
            ): part_no
            for part_no, chunk in chunks
        }
        for future in as_completed(future_to_part):
            ok, retries = future.result()
            if ok:
                uploaded += 1
            total_retries += retries
            if not ok:
                msg = f"part {future_to_part[future]} failed after {part_retries} attempts"
                raise RuntimeError(msg)
    return uploaded, total_retries


def _read_chunks(fpath: Path, chunk_size: int) -> list[tuple[int, bytes]]:
    """Read file into numbered chunks."""
    chunks: list[tuple[int, bytes]] = []
    part_no = ZERO_RETRIES
    with fpath.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            part_no += 1
            chunks.append((part_no, chunk))
    return chunks


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
) -> dict[str, Any]:
    """Attempt a single upload try. Raises on failure."""
    active_session = cfg.session or requests
    size = fpath.stat().st_size
    sha256_hash = compute_sha256(fpath)
    task_id = init_upload(
        active_session,
        cfg.base_url,
        fpath.name,
        size,
        api_key=cfg.api_key,
        timeout=cfg.timeout,
    )
    if cfg.verbose:
        logger.info("  Task ID: %s", task_id)

    chunks = _read_chunks(fpath, cfg.chunk_size)
    skip_parts = ZERO_RETRIES
    if cached and cached.get("parts_uploaded"):
        skip_parts = cached["parts_uploaded"]
        chunks = [(p, c) for p, c in chunks if p > skip_parts]
        if cfg.verbose and chunks:
            logger.info("  Resuming from part %d", skip_parts + 1)

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
        if cfg.chunk_concurrency > MIN_CONCURRENCY and len(chunks) > MIN_CONCURRENCY:
            uploaded_parts, chunk_retries = _upload_concurrent(
                active_session,
                base_url=cfg.base_url,
                task_id=task_id,
                fname=fpath.name,
                chunks=chunks,
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
                chunks=chunks,
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

    file_id = finalize_upload(
        active_session,
        cfg.base_url,
        task_id,
        api_key=cfg.api_key,
        timeout=cfg.timeout,
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
    if _is_cached_complete(
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
            )
        except (requests.RequestException, RuntimeError) as e:  # noqa: PERF203
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


def load_state(state_file: Path) -> dict[str, dict[str, Any]]:
    """Load a simple state file mapping paths to empty dicts."""
    if not state_file.exists():
        return {}
    with state_file.open() as f:
        return {line.strip(): {} for line in f if line.strip()}


def save_state(state_file: Path, uploaded: set[str]) -> None:
    """Save uploaded paths to a state file."""
    with state_file.open("w") as f:
        f.writelines(path + "\n" for path in sorted(uploaded))


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
    except (RuntimeError, requests.RequestException):
        logger.exception("  ERROR")
        return None


def _cfg_from_args(
    args: argparse.Namespace,
    *,
    session: requests.Session | None = None,
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


def _resolve_args(args: argparse.Namespace, config: Config) -> None:
    """Resolve argument defaults from config and environment."""
    args.base_url = args.base_url or config.get("base_url") or BASE_URL
    args.api_key = args.api_key or os.environ.get(ENV_API_KEY) or config.get("api_key")
    args.chunk_size = _resolve_default(args.chunk_size, DEFAULT_CHUNK_SIZE)
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
    args.format = _resolve_default(args.format, DEFAULT_FORMAT)
    if args.no_csv:
        args.format = NONE_FORMAT
    args.output = args.output or DEFAULT_OUTPUT_PATTERN.format(
        ext=args.format if args.format != NONE_FORMAT else CSV_EXT,
    )
    args.state_file = _resolve_default(args.state_file, DEFAULT_STATE_FILE)
    if args.quiet:
        args.verbose = False


def _get_uploaded_set(state: StateFile | None) -> set[str]:
    """Get set of already-uploaded paths from state."""
    uploaded: set[str] = set()
    if state:
        for path, entry in state.entries.items():
            has_url = entry.get("url")
            is_empty = len(entry) == MIN_CONCURRENCY and entry.get("path")
            if has_url or is_empty:
                uploaded.add(path)
    return uploaded


def _filter_already_uploaded(files: list[Path], uploaded: set[str]) -> list[Path]:
    """Filter out files that have already been uploaded."""
    return [f for f in files if str(f.resolve()) not in uploaded]


def _do_upload_main(  # noqa: PLR0913
    fpath: Path,
    session: requests.Session,
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
    except (RuntimeError, requests.RequestException):
        logger.exception("  ERROR")
        return None
    else:
        if show_output:
            _log_upload_result(result, verbose=cfg.verbose)
        return result


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


def _write_output(
    results: list[dict[str, Any]],
    *,
    fmt: str,
    output_path: str,
    show_output: bool,
) -> None:
    """Write output file and log the path."""
    if fmt != NONE_FORMAT and results:
        out = Path(output_path)
        writer = OutputWriter(fmt, str(out))
        with writer:
            for r in results:
                writer.write(r)
        if show_output:
            logger.info("\nOutput: %s", out.resolve())
    elif show_output and not results:
        logger.info("\nNo results to write.")


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


def _run_concurrent_uploads(  # noqa: PLR0913
    files: list[Path],
    sessions: list[requests.Session],
    args: argparse.Namespace,
    *,
    state: StateFile | None,
    progress: bool,
    show_output: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Run uploads concurrently and return (results, error_count)."""
    results: list[dict[str, Any]] = []
    errors = ZERO_RETRIES
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures: dict[Any, Path] = {}
        for i, fpath in enumerate(files):
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
            except (RuntimeError, requests.RequestException):  # noqa: PERF203
                errors += 1
                logger.exception("  ERROR")
            else:
                if result:
                    results.append(result)
                else:
                    errors += 1
    return results, errors


def _run_sequential_uploads(  # noqa: PLR0913
    files: list[Path],
    sessions: list[requests.Session],
    args: argparse.Namespace,
    *,
    state: StateFile | None,
    progress: bool,
    show_output: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Run uploads sequentially and return (results, error_count)."""
    results: list[dict[str, Any]] = []
    errors = ZERO_RETRIES
    session = sessions[ZERO_RETRIES] if sessions else None
    for fpath in files:
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
        else:
            errors += 1
    return results, errors


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


def _run_upload_loop(  # noqa: PLR0913
    files: list[Path],
    sessions: list[requests.Session],
    args: argparse.Namespace,
    *,
    state: StateFile | None,
    progress: bool,
    show_output: bool,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Run the upload loop. Returns (results, errors, interrupted)."""
    results: list[dict[str, Any]] = []
    errors = ZERO_RETRIES
    try:
        if args.concurrency > MIN_CONCURRENCY:
            results, errors = _run_concurrent_uploads(
                files,
                sessions,
                args,
                state=state,
                progress=progress,
                show_output=show_output,
            )
        else:
            results, errors = _run_sequential_uploads(
                files,
                sessions,
                args,
                state=state,
                progress=progress,
                show_output=show_output,
            )
        interrupted = False
    except KeyboardInterrupt:
        if show_output:
            logger.info("\nInterrupted - saving progress...")
        interrupted = True
    return results, errors, interrupted


def _run_lib_upload_loop(  # noqa: C901
    files: list[Path],
    sessions: list[requests.Session],
    cfg: UploadConfig,
    *,
    state: StateFile | None,
    concurrency: int,
) -> list[dict[str, Any]]:
    """Run uploads for the library API. Returns results."""
    results: list[dict[str, Any]] = []

    def do_upload_lib(
        fpath: Path,
        session: requests.Session,
    ) -> dict[str, Any] | None:
        """Upload a single file from the library API."""
        lib_cfg = UploadConfig(
            **{**cfg.__dict__, "session": session, "state": state},
        )
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
                    except (RuntimeError, requests.RequestException):  # noqa: PERF203
                        pass
                    else:
                        if result:
                            results.append(result)
        else:
            session = sessions[ZERO_RETRIES] if sessions else None
            for fpath in files:
                try:
                    result = do_upload_lib(fpath, session)
                except (RuntimeError, requests.RequestException):  # noqa: PERF203
                    pass
                else:
                    if result:
                        results.append(result)
    finally:
        for s in sessions:
            s.close()
    return results


def main(argv: list[str] | None = None) -> int:  # noqa: C901, PLR0911
    """Run the CLI entry point."""
    args_list = argv if argv is not None else sys.argv[1:]
    if args_list and args_list[0] == "download":
        return _cmd_download(args_list[1:])

    args = parse_args(argv)

    if args.completions:
        print_completions(args.completions)
        return ZERO_RETRIES

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
    sessions = [requests.Session() for _ in range(max(args.concurrency, MIN_CONCURRENCY))]
    overall_start = time.time()

    results, errors, interrupted = _run_upload_loop(
        files,
        sessions,
        args,
        state=state,
        progress=progress,
        show_output=show_output,
    )
    if interrupted:
        return _handle_keyboard_interrupt(results, state)

    for s in sessions:
        s.close()

    _write_output(
        results,
        fmt=args.format,
        output_path=args.output,
        show_output=show_output,
    )

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

    sessions = [requests.Session() for _ in range(max(concurrency, MIN_CONCURRENCY))]
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


if __name__ == "__main__":
    sys.exit(main())
