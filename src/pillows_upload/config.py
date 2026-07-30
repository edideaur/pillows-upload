"""Configuration loading for pillows-upload."""

# Copyright (c) 2024 pillows-upload contributors
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib: Any = None


class Config:
    """Load configuration from TOML or KEY=VALUE config files."""

    USER_CONFIG_DIR = Path.home() / ".config" / "pillows-upload"
    USER_CONFIG_FILE = USER_CONFIG_DIR / "config"

    def __init__(self, explicit_path: str | None = None) -> None:
        """Load config from explicit path or user config directory."""
        self.data: dict[str, str] = {}
        self.path = Path(explicit_path) if explicit_path else self.USER_CONFIG_FILE
        self._load_file(self.path)

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

    def set(self, key: str, value: str) -> None:
        """Set a config value in memory (call ``save`` to persist)."""
        self.data[key] = str(value)

    def save(self, path: str | None = None) -> Path:
        """Persist config as a KEY=VALUE file, creating parent dirs."""
        target = Path(path) if path else self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{key}={value}" for key, value in sorted(self.data.items())]
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target
