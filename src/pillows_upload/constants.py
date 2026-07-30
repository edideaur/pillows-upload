"""Constants and version for pillows-upload."""

# Copyright (c) 2026 edideaur
# SPDX-License-Identifier: MIT

from __future__ import annotations

import re
from typing import TypeVar

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
KB_DIVISOR = 1024
MB_DIVISOR = 1024 * 1024
READ_CHUNK_SIZE = 4 * 1024 * 1024
DRY_RUN_PREFIX = "DRY_RUN_"
PILLOWS_URL_TEMPLATE = "https://pillows.su/f/{file_id}"
ENV_API_KEY = "PILLOWS_KEY"
DEFAULT_STATE_FILE = ".upload_state"
IMGUR_STATE_FILE = ".imgur_upload_state"
DEFAULT_FORMAT = "csv"
DEFAULT_OUTPUT_PATTERN = "upload_map.{ext}"
NONE_FORMAT = "none"
CSV_EXT = "csv"
__version__ = "0.1.0"

CLONR_URL_PATTERN = re.compile(r"https?://clonr\.co/(.+)")
IMGGUR_FILE_URL_PATTERN = re.compile(r"https?://imgur\.gg/f/([a-zA-Z0-9]+)")
IMGGUR_CDN_API = "https://imgur.gg/api/file/{file_id}"

IMGUR_BASE_URL = "https://imgur.gg"
IMGUR_UPLOAD_ENDPOINT = "/api/upload"
IMGUR_COMPLETE_ENDPOINT = "/api/upload/complete"
IMGUR_FILE_URL_TEMPLATE = "https://imgur.gg/f/{file_id}"
ENV_IMGUR_KEY = "IMGUR_KEY"
IMGUR_DEFAULT_PART_SIZE = 8 * 1024 * 1024
IMGUR_MAX_FILES_PER_REQUEST = 50
