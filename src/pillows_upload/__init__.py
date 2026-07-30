"""pillows-upload: Bulk upload files to pillows.su."""

# Copyright 2026 edideaur
# SPDX-License-Identifier:  SSPL-1.0

from __future__ import annotations

from .config import Config
from .constants import __version__
from .state import StateFile
from .upload import UploadConfig, upload_files, upload_one

__all__ = [
    "Config",
    "StateFile",
    "UploadConfig",
    "__version__",
    "upload_files",
    "upload_one",
]
