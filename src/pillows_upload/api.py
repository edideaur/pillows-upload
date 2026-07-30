"""HTTP API functions for pillows-upload."""

# Copyright (c) 2026 edideaur
# SPDX-License-Identifier: MIT

from __future__ import annotations

import niquests  # noqa: TC002

from .utils import _headers


def init_upload(  # noqa: PLR0913
    session: niquests.Session,
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
    session: niquests.Session,
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
        files=files,  # ty: ignore[invalid-argument-type]  # niquests stub under-resolves nested tuple alias
        data=data,
        headers=_headers(api_key),
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        msg = f"part {part_no} failed: {resp.text}"
        raise RuntimeError(msg)


def finalize_upload(
    session: niquests.Session,
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
