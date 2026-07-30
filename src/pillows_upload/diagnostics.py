"""Diagnostics: circuit breaker, API-key pre-flight check, and ``doctor``."""

# Copyright (c) 2026 edideaur
# SPDX-License-Identifier: MIT

from __future__ import annotations

import logging
import time

import niquests

from .config import Config
from .utils import make_session

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Stop hammering a host after repeated consecutive failures.

    Once ``failure_threshold`` consecutive failures occur the breaker opens and
    ``allow()`` returns ``False`` (fail-fast) until ``cooldown`` seconds elapse,
    after which it resets. A single success clears the failure count.
    """

    def __init__(self, failure_threshold: int = 5, cooldown: float = 30.0) -> None:
        """``failure_threshold`` consecutive failures open the breaker."""
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self._failures = 0
        self._opened_at = 0.0
        self.tripped = False

    def record_success(self) -> None:
        """Reset the failure count after a successful operation."""
        self._failures = 0
        self.tripped = False

    def record_failure(self) -> None:
        """Record a failure and open the breaker once the threshold is hit."""
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self.tripped = True
            self._opened_at = time.time()
            logger.error(
                "Circuit breaker OPEN after %d failures; pausing %ss",
                self._failures,
                self.cooldown,
            )

    def allow(self) -> bool:
        """Return True if a request may proceed, else False while open."""
        if not self.tripped:
            return True
        if time.time() - self._opened_at >= self.cooldown:
            self.tripped = False
            self._failures = 0
            logger.info("Circuit breaker reset; resuming")
            return True
        return False


def verify_api_key(
    base_url: str,
    api_key: str | None,
    *,
    timeout: int = 10,
) -> tuple[bool, str]:
    """Best-effort check that the API key is accepted by the host.

    Returns ``(ok, message)``. A 401/403 with a key present means the key is
    rejected; connection errors mean the host is unreachable. Other responses
    are treated as success (the endpoint may not expose a status route).
    """
    if not api_key:
        return False, "no API key provided"
    session = make_session()
    try:
        resp = session.get(
            base_url,
            headers={"x-api-key": api_key},
            timeout=timeout,
            allow_redirects=True,
        )
    except niquests.RequestException as e:
        return False, f"network error reaching {base_url}: {e}"
    finally:
        session.close()
    if resp.status_code in (401, 403):
        return False, f"API key rejected (HTTP {resp.status_code})"
    return True, f"reachable (HTTP {resp.status_code})"


def run_doctor(
    *,
    api_key: str | None,
    base_url: str,
    imgur_key: str | None,
    imgur_base_url: str,
    timeout: int = 10,
) -> int:
    """Run environment diagnostics and print a report. Returns exit code."""
    rows: list[tuple[str, bool, str]] = []

    cfg_path = Config().USER_CONFIG_FILE
    rows.append(("config file", cfg_path.exists(), str(cfg_path)))
    rows.append(("pillows API key", bool(api_key), (api_key and "set") or "missing (set PILLOWS_KEY)"))

    ok, msg = verify_api_key(base_url, api_key, timeout=timeout)
    rows.append(("pillows reachable", ok, msg))

    session = make_session()
    proto = "unknown"
    try:
        resp = session.get(base_url, timeout=timeout, allow_redirects=True)
        proto = getattr(resp, "http_version", "unknown")
    except niquests.RequestException as e:
        proto = f"error: {e}"
    finally:
        session.close()
    proto_ok = str(proto).startswith("HTTP/")
    rows.append(("pillows HTTP protocol", proto_ok, f"negotiated {proto}"))

    rows.append(("imgur API key", bool(imgur_key), (imgur_key and "set") or "missing (set IMGUR_KEY)"))

    ik_ok, ik_msg = verify_api_key(imgur_base_url, imgur_key, timeout=timeout)
    rows.append(("imgur reachable", ik_ok, ik_msg))

    width = max(len(name) for name, _, _ in rows)
    failed = 0
    for name, ok, msg in rows:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        logger.info("%-*s  [%s]  %s", width, name, mark, msg)

    if failed:
        logger.info("\n%d check(s) failed", failed)
        return 1
    logger.info("\nAll checks passed")
    return 0
