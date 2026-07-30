"""State file management for pillows-upload."""

# Copyright (c) 2024 pillows-upload contributors
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003
from threading import Lock
from typing import Any

from .constants import MIN_CONCURRENCY


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
        """Record upload metadata for a path and persist to disk.

        Uses append-only writes so recording N files is O(N) rather than the
        O(N**2) of rewriting the whole file after every file.
        """
        with self.lock:
            entry = {"path": path, **kwargs}
            self.entries[path] = entry
            with self.path.open("a") as f:
                f.write(json.dumps(entry) + "\n")


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
