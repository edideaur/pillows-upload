"""Tests for new operational features (no network required)."""

# Copyright (c) 2026 edideaur
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

from pillows_upload.config import Config
from pillows_upload.constants import DEFAULT_STATE_FILE, IMGUR_STATE_FILE
from pillows_upload.diagnostics import CircuitBreaker, verify_api_key
from pillows_upload.imgur import _imgur_resume_filter
from pillows_upload.logutils import _JSONFormatter
from pillows_upload.state import StateFile
from pillows_upload.utils import make_session

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_circuit_breaker_trips_and_resets() -> None:
    """Breaker opens after threshold and clears on a later success."""
    cb = CircuitBreaker(failure_threshold=3, cooldown=30.0)
    assert cb.allow()
    cb.record_failure()
    cb.record_failure()
    assert cb.allow()
    cb.record_failure()
    assert cb.tripped
    assert not cb.allow()
    cb.record_success()
    assert not cb.tripped
    assert cb.allow()


def test_config_set_save_roundtrip(tmp_path: Path) -> None:
    """A saved config value is visible when reloaded."""
    cfg = Config(str(tmp_path / "c"))
    cfg.set("PILLOWS_KEY", "abc")
    target = cfg.save()
    assert target.is_file()
    assert Config(str(target)).get("PILLOWS_KEY") == "abc"


def test_make_session_adds_telemetry_hook() -> None:
    """make_session attaches a response hook for protocol telemetry."""
    session = make_session()
    hooks = session.hooks.get("response")
    assert isinstance(hooks, list)
    assert len(hooks) == 1


def test_json_formatter_emits_object() -> None:
    """The JSON formatter renders a parseable object with level and message."""
    formatter = _JSONFormatter()
    record = logging.LogRecord("mod", logging.INFO, "path", 1, "hello %s", ("world",), None)
    payload = json.loads(formatter.format(record))
    assert payload["level"] == "INFO"
    assert payload["msg"] == "hello world"


def test_verify_api_key_rejects_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 from the host is reported as a rejected key."""

    class FakeResp:
        status_code = 401

    class FakeSession:
        def get(self, *_args: object, **_kwargs: object) -> FakeResp:
            return FakeResp()

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "pillows_upload.diagnostics.make_session",
        lambda: FakeSession(),  # noqa: PLW0108
    )
    ok, msg = verify_api_key("https://example.test", "key")
    assert ok is False
    assert "401" in msg


def test_imgur_resume_filter_skips_cached(tmp_path: Path) -> None:
    """Files already recorded in the state file are filtered out as done."""
    f = tmp_path / "a.bin"
    f.write_bytes(b"data")
    state = StateFile(tmp_path / ".state")
    sha = hashlib.sha256(b"data").hexdigest()
    state.record(str(f.resolve()), size=4, sha256=sha, parts_uploaded=1, url="https://imgur.gg/f/DONE")

    cfg = ImgurConfigForTest(state)
    todo, done = _imgur_resume_filter(cfg, [f])
    assert todo == []
    assert len(done) == 1


class ImgurConfigForTest:
    """Minimal stand-in exposing only the ``state`` attribute used by the filter."""

    def __init__(self, state: StateFile) -> None:
        """Store the state file used by the resume filter."""
        self.state = state


def test_imgur_state_file_distinct_from_pillows() -> None:
    """Imgur must not share pillows.su's default resume state file."""
    assert DEFAULT_STATE_FILE != IMGUR_STATE_FILE


def test_state_file_append_only(tmp_path: Path) -> None:
    """Each record is appended, and reloading yields all entries."""
    expected_lines = 2
    sf = StateFile(tmp_path / ".state")
    sf.record("a", url="u1", size=1, sha256="x", parts_uploaded=1)
    sf.record("b", url="u2", size=2, sha256="y", parts_uploaded=1)

    lines = (tmp_path / ".state").read_text().strip().splitlines()
    assert len(lines) == expected_lines
    reloaded = StateFile(tmp_path / ".state")
    assert reloaded.get("a")["url"] == "u1"
    assert reloaded.get("b")["url"] == "u2"


def test_state_file_dedup_on_reload(tmp_path: Path) -> None:
    """Re-recording the same path keeps the latest entry after reload."""
    sf = StateFile(tmp_path / ".state")
    sf.record("a", url="u1", size=1, sha256="x", parts_uploaded=1)
    sf.record("a", url="u1b", size=1, sha256="x", parts_uploaded=1)

    reloaded = StateFile(tmp_path / ".state")
    assert reloaded.get("a")["url"] == "u1b"
