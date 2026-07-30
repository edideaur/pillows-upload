"""Output writing for pillows-upload results."""

# Copyright (c) 2026 edideaur
# SPDX-License-Identifier: MIT

from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from typing_extensions import Self

if TYPE_CHECKING:
    import types

try:
    import openpyxl
except ImportError:
    openpyxl: Any = None

from .constants import CSV_EXT


class OutputWriter:
    """Context manager for writing upload results to various formats."""

    def __init__(self, fmt: str, path: str, link_key: str = "pillows_su_link") -> None:
        """Initialize with output format and file path.

        ``link_key`` is the result dict key holding the uploaded file URL
        (defaults to ``pillows_su_link``; imgur uploads use ``imgur_link``).
        """
        self.fmt = fmt
        self.path = path
        self.link_key = link_key
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
                    fieldnames=["file_path", self.link_key],
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
                {"file_path": result["file_path"], self.link_key: result[self.link_key]},
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
                f"<html><body><table><tr><th>file_path</th><th>{self.link_key}</th></tr>",
            )
            for r in self._results:
                link = r[self.link_key]
                f.write(
                    f"<tr><td>{html.escape(r['file_path'])}</td>"
                    f"<td><a href='{html.escape(link)}'>{html.escape(link)}</a></td></tr>",
                )
            f.write("</table></body></html>")

    def _write_xlsx(self) -> None:
        """Write results as XLSX spreadsheet."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["file_path", self.link_key])
        for r in self._results:
            ws.append([r["file_path"], r[self.link_key]])
        wb.save(self.path)
