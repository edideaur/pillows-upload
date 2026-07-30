"""``config`` subcommand for reading and writing pillows-upload configuration."""

# Copyright (c) 2026 edideaur
# SPDX-License-Identifier: MIT

from __future__ import annotations

import argparse
import logging

from .config import Config

logger = logging.getLogger(__name__)

USAGE = "pillows-upload config {get KEY | set KEY VALUE | list | path} [--config PATH]"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pillows-upload config", usage=USAGE)
    p.add_argument("action", choices=["get", "set", "list", "path"])
    p.add_argument("key", nargs="?", help="config key (for get/set)")
    p.add_argument("value", nargs="?", help="config value (for set)")
    p.add_argument("--config", default=None, help="config file path")
    return p


def _cmd_config(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    """Handle the ``config`` subcommand: get/set/list configuration values."""
    args = _build_parser().parse_args(argv)

    if args.action in ("get", "set") and not args.key:
        logger.error("'key' is required for get/set")
        return 2
    if args.action == "set" and args.value is None:
        logger.error("'value' is required for set")
        return 2

    config = Config(args.config)

    if args.action == "get":
        value = config.get(args.key)
        if value is None:
            logger.error("no such key: %s", args.key)
            return 1
        logger.info("%s", value)
        return 0

    if args.action == "set":
        config.set(args.key, args.value)
        target = config.save()
        logger.info("Saved %s -> %s", args.key, target)
        return 0

    if args.action == "path":
        logger.info("%s", config.path)
        return 0

    for key, value in sorted(config.data.items()):
        logger.info("%s=%s", key, value)
    return 0
