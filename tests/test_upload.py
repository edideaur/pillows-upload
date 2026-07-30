# Copyright (c) 2026 edideaur
# SPDX-License-Identifier: MIT
"""Tests for the pillows-upload CLI and library."""

from __future__ import annotations

import argparse
import csv
import json
import logging
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import niquests
import pytest

from pillows_upload.api import finalize_upload, init_upload, upload_part
from pillows_upload.cli import main, parse_args, print_completions, validate_args
from pillows_upload.config import Config
from pillows_upload.download import (
    _cmd_download,
    _download_file,
    _http_get_json,
    _resolve_download_url,
)
from pillows_upload.output import OutputWriter
from pillows_upload.state import StateFile, load_state, save_state
from pillows_upload.upload import UploadConfig, upload_files, upload_one, upload_task
from pillows_upload.utils import _headers, collect_files, compute_sha256

if TYPE_CHECKING:
    from pathlib import Path


class TestHeaders:
    """Tests for the _headers helper function."""

    def test_with_key(self) -> None:
        """Headers include API key when provided."""
        assert _headers("abc123") == {"x-api-key": "abc123"}

    def test_without_key(self) -> None:
        """Headers are empty when no key is provided."""
        assert _headers(None) == {}

    def test_empty_string(self) -> None:
        """Headers are empty when key is an empty string."""
        assert _headers("") == {}


class TestComputeSha256:
    """Tests for the compute_sha256 function."""

    def test_returns_hex_digest(self, tmp_path: Path) -> None:
        """Returns a 64-character hex digest for a known input."""
        f = tmp_path / "file.bin"
        f.write_bytes(b"hello world")
        result = compute_sha256(f)
        assert len(result) == 64  # noqa: PLR2004
        assert result == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"

    def test_empty_file(self, tmp_path: Path) -> None:
        """Returns the SHA-256 of an empty file."""
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        result = compute_sha256(f)
        assert len(result) == 64  # noqa: PLR2004
        assert result == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def test_deterministic(self, tmp_path: Path) -> None:
        """Hashing the same file twice produces the same result."""
        f = tmp_path / "file.bin"
        f.write_bytes(b"\x00" * 1024)
        assert compute_sha256(f) == compute_sha256(f)

    def test_large_file_deterministic(self, tmp_path: Path) -> None:
        """Hashing a larger file is deterministic."""
        f = tmp_path / "large.bin"
        f.write_bytes(b"ab" * (4 * 1024 * 1024))
        result = compute_sha256(f)
        assert len(result) == 64  # noqa: PLR2004
        assert result == compute_sha256(f)


class TestConfig:
    """Tests for the Config class."""

    def test_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config reads key-value pairs from the user config file."""
        config_file = tmp_path / "config"
        config_file.write_text("PILLOWS_KEY=envkey123\nBASE_URL=https://custom.api\n")
        monkeypatch.setattr(Config, "USER_CONFIG_FILE", config_file)
        config = Config()
        assert config.get("PILLOWS_KEY") == "envkey123"
        assert config.get("BASE_URL") == "https://custom.api"

    def test_explicit_config_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config loads from an explicit file path."""
        env = tmp_path / "myconfig.env"
        env.write_text("MY_KEY=explicit\n")
        monkeypatch.setattr(Config, "USER_CONFIG_FILE", tmp_path / "nonexistent")
        config = Config(str(env))
        assert config.get("MY_KEY") == "explicit"

    def test_missing_config_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config returns None for missing keys when no config file exists."""
        monkeypatch.setattr(Config, "USER_CONFIG_FILE", tmp_path / "nonexistent")
        config = Config()
        assert config.get("anything") is None

    def test_env_file_comments_and_quotes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config skips comments and strips quotes from values."""
        config_file = tmp_path / "config"
        config_file.write_text('# comment\nKEY="quoted"\nKEY2=noquotes\n')
        monkeypatch.setattr(Config, "USER_CONFIG_FILE", config_file)
        config = Config()
        assert config.get("KEY") == "quoted"
        assert config.get("KEY2") == "noquotes"

    def test_env_file_skips_blank_lines(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config skips blank lines in config files."""
        config_file = tmp_path / "config"
        config_file.write_text("\n\nKEY=value\n\n")
        monkeypatch.setattr(Config, "USER_CONFIG_FILE", config_file)
        config = Config()
        assert config.get("KEY") == "value"

    def test_config_explicit_key_value_path(self, tmp_path: Path) -> None:
        """Config loads from an explicit KEY=VALUE path."""
        config_file = tmp_path / "myconfig.env"
        config_file.write_text('apikey = "envpath"\n')
        config = Config(str(config_file))
        assert config.get("apikey") == "envpath"

    def test_toml_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config reads from [pillows-upload] section in TOML."""
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[pillows-upload]\napi_key = "toml123"\nbase_url = "https://toml.api"\n')
        monkeypatch.setattr(Config, "USER_CONFIG_FILE", tmp_path / "nonexistent")
        config = Config(str(toml_file))
        assert config.get("api_key") == "toml123"
        assert config.get("base_url") == "https://toml.api"

    def test_toml_tool_section(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config reads from [tool.pillows-upload] section in TOML."""
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[tool.pillows-upload]\napi_key = "toolkey"\n')
        monkeypatch.setattr(Config, "USER_CONFIG_FILE", tmp_path / "nonexistent")
        config = Config(str(toml_file))
        assert config.get("api_key") == "toolkey"

    def test_missing_toml_module(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config gracefully handles missing tomllib."""
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[pillows-upload]\napi_key = "key"\n')
        monkeypatch.setattr(Config, "USER_CONFIG_FILE", tmp_path / "nonexistent")
        monkeypatch.setattr("pillows_upload.config.tomllib", None)
        config = Config(str(toml_file))
        assert config.get("api_key") is None


class TestStateFile:
    """Tests for the StateFile class."""

    def test_new_file_is_empty(self, tmp_path: Path) -> None:
        """A new state file has no entries."""
        state = StateFile(tmp_path / "state")
        assert state.get("anything") is None

    def test_plain_text_backward_compat(self, tmp_path: Path) -> None:
        """StateFile reads plain-text path-per-line format."""
        state_file = tmp_path / "state"
        state_file.write_text("/a/file1.mp3\n/file2.wav\n")
        state = StateFile(state_file)
        assert state.get("/a/file1.mp3")["path"] == "/a/file1.mp3"
        assert state.get("/file2.wav")["path"] == "/file2.wav"

    def test_jsonl_format(self, tmp_path: Path) -> None:
        """StateFile reads JSONL format entries."""
        state_file = tmp_path / "state"
        state_file.write_text(
            '{"path": "/a.mp3", "size": 100, "sha256": "abc"}\n{"path": "/b.wav", "size": 200, "sha256": "def"}\n',
        )
        state = StateFile(state_file)
        assert state.get("/a.mp3")["size"] == 100  # noqa: PLR2004
        assert state.get("/b.wav")["sha256"] == "def"

    def test_record_and_persist(self, tmp_path: Path) -> None:
        """Recording an entry persists it to disk."""
        state_file = tmp_path / "state"
        state = StateFile(state_file)
        state.record("/a.mp3", size=100, sha256="abc", url="https://example.com/a")
        with state_file.open() as f:
            entries = [json.loads(line.strip()) for line in f]
        assert len(entries) == 1
        assert entries[0]["path"] == "/a.mp3"
        assert entries[0]["url"] == "https://example.com/a"

    def test_atomic_write(self, tmp_path: Path) -> None:
        """State file writes are atomic (no .tmp file left behind)."""
        state_file = tmp_path / "state"
        state = StateFile(state_file)
        state.record("/a.mp3", size=100)
        assert not state_file.with_suffix(".tmp").exists()

    def test_overwrites_entry(self, tmp_path: Path) -> None:
        """Recording the same path overwrites the previous entry."""
        state_file = tmp_path / "state"
        state = StateFile(state_file)
        state.record("/a.mp3", size=100, url="old")
        state.record("/a.mp3", size=100, url="new")
        assert state.get("/a.mp3")["url"] == "new"

    def test_mixed_plain_and_jsonl(self, tmp_path: Path) -> None:
        """StateFile handles a mix of plain-text and JSONL lines."""
        state_file = tmp_path / "state"
        state_file.write_text('/plain/path.mp3\n{"path": "/json/path.wav", "size": 200}\n')
        state = StateFile(state_file)
        assert state.get("/plain/path.mp3")["path"] == "/plain/path.mp3"
        assert state.get("/json/path.wav")["size"] == 200  # noqa: PLR2004

    def test_record_persists_to_disk(self, tmp_path: Path) -> None:
        """Recorded entries are written to disk with expected fields."""
        state_file = tmp_path / "state"
        state = StateFile(state_file)
        state.record("/a.mp3", size=100, parts_uploaded=3)
        with state_file.open() as f:
            lines = f.readlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["parts_uploaded"] == 3  # noqa: PLR2004


class TestOutputWriter:
    """Tests for the OutputWriter class."""

    def test_csv_streaming(self, tmp_path: Path) -> None:
        """CSV output writes rows immediately."""
        out = tmp_path / "out.csv"
        with OutputWriter("csv", str(out)) as writer:
            writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})
            writer.write({"file_path": "/b.wav", "pillows_su_link": "https://x.com/b"})
        with out.open() as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 2  # noqa: PLR2004
        assert rows[0]["file_path"] == "/a.mp3"

    def test_ndjson_streaming(self, tmp_path: Path) -> None:
        """NDJSON output writes lines immediately."""
        out = tmp_path / "out.ndjson"
        with OutputWriter("ndjson", str(out)) as writer:
            writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})
        with out.open() as f:
            line = f.readline()
            obj = json.loads(line)
        assert obj["file_path"] == "/a.mp3"

    def test_json_buffered(self, tmp_path: Path) -> None:
        """JSON output buffers results and writes on close."""
        out = tmp_path / "out.json"
        with OutputWriter("json", str(out)) as writer:
            writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})
        with out.open() as f:
            data = json.load(f)
        assert len(data) == 1
        assert data[0]["file_path"] == "/a.mp3"

    def test_html_buffered(self, tmp_path: Path) -> None:
        """HTML output writes a table on close."""
        out = tmp_path / "out.html"
        with OutputWriter("html", str(out)) as writer:
            writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})
        content = out.read_text()
        assert "<table>" in content
        assert "/a.mp3" in content

    def test_xlsx_requires_openpyxl(self, tmp_path: Path) -> None:
        """XLSX output raises RuntimeError when openpyxl is not installed."""
        out = tmp_path / "out.xlsx"
        with patch("pillows_upload.output.openpyxl", None):
            writer = OutputWriter("xlsx", str(out))
            with pytest.raises(RuntimeError, match="openpyxl is required"):
                writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})

    def test_json_multiple_results(self, tmp_path: Path) -> None:
        """JSON output contains all written results."""
        out = tmp_path / "out.json"
        with OutputWriter("json", str(out)) as writer:
            writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})
            writer.write({"file_path": "/b.wav", "pillows_su_link": "https://x.com/b"})
        with out.open() as f:
            data = json.load(f)
        assert len(data) == 2  # noqa: PLR2004
        assert data[0]["file_path"] == "/a.mp3"

    def test_html_contains_links(self, tmp_path: Path) -> None:
        """HTML output includes clickable links."""
        out = tmp_path / "out.html"
        with OutputWriter("html", str(out)) as writer:
            writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})
        content = out.read_text()
        assert "<a href='https://x.com/a'>" in content

    def test_ndjson_multiple_lines(self, tmp_path: Path) -> None:
        """NDJSON output has one JSON object per line."""
        out = tmp_path / "out.ndjson"
        with OutputWriter("ndjson", str(out)) as writer:
            writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})
            writer.write({"file_path": "/b.wav", "pillows_su_link": "https://x.com/b"})
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 2  # noqa: PLR2004
        assert json.loads(lines[1])["file_path"] == "/b.wav"

    def test_csv_header_matches_fields(self, tmp_path: Path) -> None:
        """CSV header matches the expected field names."""
        out = tmp_path / "out.csv"
        with OutputWriter("csv", str(out)) as writer:
            writer.write({"file_path": "/a.mp3", "pillows_su_link": "https://x.com/a"})
        with out.open() as f:
            header = f.readline().strip()
        assert header == "file_path,pillows_su_link"


class TestParseArgs:
    """Tests for the parse_args function."""

    def test_defaults(self) -> None:
        """Default arguments match expected values."""
        args = parse_args([])
        assert args.paths == ["./downloads"]
        assert args.output is None
        assert args.api_key is None
        assert args.base_url is None
        assert args.chunk_size is None
        assert args.concurrency is None
        assert args.chunk_concurrency is None
        assert args.retries is None
        assert args.part_retries is None
        assert args.backoff is None
        assert args.timeout is None
        assert args.dry_run is False
        assert args.verbose is False
        assert args.quiet is False
        assert args.resume is False
        assert args.no_csv is False
        assert args.delete is False
        assert args.no_progress is False
        assert args.min_size is None
        assert args.max_size is None
        assert args.format is None

    def test_custom_paths(self) -> None:
        """Paths are parsed from positional arguments."""
        args = parse_args(["song.mp3", "album/"])
        assert args.paths == ["song.mp3", "album/"]

    def test_flags(self) -> None:
        """Boolean flags are parsed correctly."""
        args = parse_args(["--dry-run", "-v", "--resume", "--no-csv", "--delete", "--no-progress", "-q"])
        assert args.dry_run is True
        assert args.verbose is True
        assert args.resume is True
        assert args.no_csv is True
        assert args.delete is True
        assert args.no_progress is True
        assert args.quiet is True

    def test_options(self) -> None:
        """Option arguments are parsed with correct types."""
        args = parse_args(
            [
                "-o",
                "out.csv",
                "-k",
                "mykey",
                "-c",
                "4",
                "-r",
                "5",
                "--backoff",
                "3",
                "--timeout",
                "60",
                "--chunk-size",
                "1048576",
                "--min-size",
                "100",
                "--max-size",
                "9999",
                "--state-file",
                ".my_state",
                "--chunk-concurrency",
                "2",
                "--part-retries",
                "4",
                "--format",
                "json",
            ],
        )
        assert args.output == "out.csv"
        assert args.api_key == "mykey"
        assert args.concurrency == 4  # noqa: PLR2004
        assert args.chunk_concurrency == 2  # noqa: PLR2004
        assert args.retries == 5  # noqa: PLR2004
        assert args.part_retries == 4  # noqa: PLR2004
        assert args.backoff == 3  # noqa: PLR2004
        assert args.timeout == 60  # noqa: PLR2004
        assert args.chunk_size == 1048576  # noqa: PLR2004
        assert args.min_size == 100  # noqa: PLR2004
        assert args.max_size == 9999  # noqa: PLR2004
        assert args.state_file == ".my_state"
        assert args.format == "json"

    def test_ext_filter(self) -> None:
        """Extension filter is parsed as a list."""
        args = parse_args(["--ext", ".mp3", ".wav"])
        assert args.ext == [".mp3", ".wav"]

    def test_base_url(self) -> None:
        """Base URL is parsed from --base-url."""
        args = parse_args(["--base-url", "https://custom.api"])
        assert args.base_url == "https://custom.api"

    def test_no_progress_default(self) -> None:
        """no_progress defaults to False."""
        args = parse_args([])
        assert args.no_progress is False

    def test_no_progress_flag(self) -> None:
        """--no-progress flag sets no_progress to True."""
        args = parse_args(["--no-progress"])
        assert args.no_progress is True

    def test_quiet_flag(self) -> None:
        """-q flag sets quiet to True."""
        args = parse_args(["-q"])
        assert args.quiet is True

    def test_format_default(self) -> None:
        """Format defaults to None."""
        args = parse_args([])
        assert args.format is None

    def test_completions_bash(self, caplog: pytest.LogCaptureFixture) -> None:
        """Bash completions are logged via logger."""
        with caplog.at_level(logging.INFO, logger="pillows_upload"):
            result = main(["--completions", "bash"])
        assert result == 0
        assert "COMPREPLY" in caplog.text
        assert "_pillows_upload" in caplog.text

    def test_completions_zsh(self, caplog: pytest.LogCaptureFixture) -> None:
        """Zsh completions are logged via logger."""
        with caplog.at_level(logging.INFO, logger="pillows_upload"):
            result = main(["--completions", "zsh"])
        assert result == 0
        assert "#compdef" in caplog.text

    def test_completions_fish(self, caplog: pytest.LogCaptureFixture) -> None:
        """Fish completions are logged via logger."""
        with caplog.at_level(logging.INFO, logger="pillows_upload"):
            result = main(["--completions", "fish"])
        assert result == 0
        assert "complete -c pillows-upload" in caplog.text

    def test_completions_invalid_shell(self) -> None:
        """Invalid shell choice causes SystemExit from argparse."""
        with pytest.raises(SystemExit):
            main(["--completions", "unknown"])


class TestCollectFiles:
    """Tests for the collect_files function."""

    def test_collects_files_in_dir(self, tmp_path: Path) -> None:
        """Collects all files in a directory."""
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "b.mp3").write_bytes(b"\x00" * 100)
        result = collect_files([str(tmp_path)], extensions=None, min_size=0, max_size=0, verbose=False)
        assert len(result) == 2  # noqa: PLR2004

    def test_collects_single_file(self, tmp_path: Path) -> None:
        """Collects a single file path."""
        f = tmp_path / "song.mp3"
        f.write_bytes(b"\x00" * 50)
        result = collect_files([str(f)], extensions=None, min_size=0, max_size=0, verbose=False)
        assert len(result) == 1
        assert result[0] == f

    def test_extension_filter(self, tmp_path: Path) -> None:
        """Only files matching the extension filter are collected."""
        (tmp_path / "a.mp3").write_bytes(b"\x00" * 10)
        (tmp_path / "b.wav").write_bytes(b"\x00" * 10)
        (tmp_path / "c.txt").write_text("hi")
        result = collect_files([str(tmp_path)], extensions=[".mp3"], min_size=0, max_size=0, verbose=False)
        assert len(result) == 1
        assert result[0].name == "a.mp3"

    def test_extension_without_dot(self, tmp_path: Path) -> None:
        """Extensions without a leading dot are normalized."""
        (tmp_path / "a.mp3").write_bytes(b"\x00" * 10)
        (tmp_path / "b.wav").write_bytes(b"\x00" * 10)
        result = collect_files([str(tmp_path)], extensions=["wav"], min_size=0, max_size=0, verbose=False)
        assert len(result) == 1
        assert result[0].name == "b.wav"

    def test_min_size_filter(self, tmp_path: Path) -> None:
        """Files smaller than min_size are excluded."""
        (tmp_path / "small.txt").write_text("hi")
        (tmp_path / "big.txt").write_text("x" * 100)
        result = collect_files([str(tmp_path)], extensions=None, min_size=50, max_size=0, verbose=False)
        assert len(result) == 1
        assert result[0].name == "big.txt"

    def test_max_size_filter(self, tmp_path: Path) -> None:
        """Files larger than max_size are excluded."""
        (tmp_path / "small.txt").write_text("hi")
        (tmp_path / "big.txt").write_text("x" * 100)
        result = collect_files([str(tmp_path)], extensions=None, min_size=0, max_size=50, verbose=False)
        assert len(result) == 1
        assert result[0].name == "small.txt"

    def test_nonexistent_path_skipped(self, tmp_path: Path) -> None:
        """Nonexistent paths are silently skipped."""
        result = collect_files([str(tmp_path / "nope")], extensions=None, min_size=0, max_size=0, verbose=False)
        assert result == []

    def test_directories_only_contain_files(self, tmp_path: Path) -> None:
        """Only files (not directories) are collected."""
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "nested.txt").write_text("x")
        (tmp_path / "top.txt").write_text("y")
        result = collect_files([str(tmp_path)], extensions=None, min_size=0, max_size=0, verbose=False)
        assert len(result) == 2  # noqa: PLR2004

    def test_sorted_output(self, tmp_path: Path) -> None:
        """Collected files are returned in sorted order."""
        (tmp_path / "c.txt").write_text("1")
        (tmp_path / "a.txt").write_text("2")
        (tmp_path / "b.txt").write_text("3")
        result = collect_files([str(tmp_path)], extensions=None, min_size=0, max_size=0, verbose=False)
        names = [f.name for f in result]
        assert names == sorted(names)

    def test_empty_dir(self, tmp_path: Path) -> None:
        """An empty directory yields no files."""
        result = collect_files([str(tmp_path)], extensions=None, min_size=0, max_size=0, verbose=False)
        assert result == []

    def test_extension_normalization(self, tmp_path: Path) -> None:
        """Extension matching is case-insensitive."""
        (tmp_path / "a.mp3").write_bytes(b"\x00" * 10)
        (tmp_path / "b.MP3").write_bytes(b"\x00" * 10)
        result = collect_files([str(tmp_path)], extensions=["mp3"], min_size=0, max_size=0, verbose=False)
        assert len(result) == 2  # noqa: PLR2004

    def test_extension_with_dot_normalization(self, tmp_path: Path) -> None:
        """Extensions with a leading dot are handled correctly."""
        (tmp_path / "a.wav").write_bytes(b"\x00" * 10)
        result = collect_files([str(tmp_path)], extensions=[".wav"], min_size=0, max_size=0, verbose=False)
        assert len(result) == 1

    def test_nonexistent_path_skipped_verbose(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Nonexistent paths log a message when verbose is True."""
        with caplog.at_level(logging.INFO, logger="pillows_upload"):
            result = collect_files(
                [str(tmp_path / "nope")],
                extensions=None,
                min_size=0,
                max_size=0,
                verbose=True,
            )
        assert result == []
        assert "not a file or directory" in caplog.text


class TestState:
    """Tests for the load_state and save_state functions."""

    def test_load_nonexistent(self, tmp_path: Path) -> None:
        """Loading a nonexistent state file returns an empty dict."""
        assert load_state(tmp_path / "nope") == {}

    def test_load_existing(self, tmp_path: Path) -> None:
        """Loading an existing state file returns path entries."""
        sf = tmp_path / "state"
        sf.write_text("/a/file1.mp3\n/file2.wav\n")
        result = load_state(sf)
        assert result == {"/a/file1.mp3": {}, "/file2.wav": {}}

    def test_load_ignores_blank_lines(self, tmp_path: Path) -> None:
        """Blank lines in state file are ignored."""
        sf = tmp_path / "state"
        sf.write_text("/a.mp3\n\n\n/b.wav\n")
        result = load_state(sf)
        assert result == {"/a.mp3": {}, "/b.wav": {}}

    def test_save_and_load(self, tmp_path: Path) -> None:
        """Saving and loading a state file round-trips correctly."""
        sf = tmp_path / "state"
        save_state(sf, {"/b.mp3", "/a.wav"})
        result = load_state(sf)
        assert result == {"/a.wav": {}, "/b.mp3": {}}

    def test_save_is_sorted(self, tmp_path: Path) -> None:
        """Saved paths are written in sorted order."""
        sf = tmp_path / "state"
        save_state(sf, {"/c", "/a", "/b"})
        lines = sf.read_text().splitlines()
        assert lines == ["/a", "/b", "/c"]


class TestInitUpload:
    """Tests for the init_upload function."""

    @patch("pillows_upload.api.niquests.post")
    def test_success(self, mock_post: MagicMock) -> None:
        """Returns the task ID on successful init."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": True, "message": {"id": "task123"}}
        mock_post.return_value = mock_resp

        session = MagicMock()
        session.post.return_value = mock_resp
        result = init_upload(session, "https://api.test", "file.mp3", 1024, api_key="key1", timeout=30)
        assert result == "task123"
        session.post.assert_called_once()

    @patch("pillows_upload.api.niquests.post")
    def test_failure(self, mock_post: MagicMock) -> None:
        """Raises RuntimeError when init response indicates failure."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": False, "message": "bad request"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        session = MagicMock()
        session.post.return_value = mock_resp
        with pytest.raises(RuntimeError, match="init failed"):
            init_upload(session, "https://api.test", "file.mp3", 1024, api_key=None, timeout=30)


class TestUploadPart:
    """Tests for the upload_part function."""

    @patch("pillows_upload.api.niquests.put")
    def test_success(self, mock_put: MagicMock) -> None:
        """upload_part completes without error on success."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": True}
        mock_put.return_value = mock_resp

        session = MagicMock()
        session.put.return_value = mock_resp
        upload_part(session, "https://api.test", "task1", "f.mp3", b"data", 1, api_key="key1", timeout=120)
        session.put.assert_called_once()

    @patch("pillows_upload.api.niquests.put")
    def test_failure(self, mock_put: MagicMock) -> None:
        """Raises RuntimeError when part upload response indicates failure."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": False}
        mock_resp.text = "error"
        mock_put.return_value = mock_resp

        session = MagicMock()
        session.put.return_value = mock_resp
        with pytest.raises(RuntimeError, match="part 1 failed"):
            upload_part(session, "https://api.test", "task1", "f.mp3", b"data", 1, api_key=None, timeout=120)


class TestFinalizeUpload:
    """Tests for the finalize_upload function."""

    @patch("pillows_upload.api.niquests.get")
    def test_success(self, mock_get: MagicMock) -> None:
        """Returns the file ID on successful finalize."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": True, "message": {"id": "file99"}}
        mock_get.return_value = mock_resp

        session = MagicMock()
        session.get.return_value = mock_resp
        result = finalize_upload(session, "https://api.test", "task1", api_key="key1", timeout=300)
        assert result == "file99"

    @patch("pillows_upload.api.niquests.get")
    def test_failure(self, mock_get: MagicMock) -> None:
        """Raises RuntimeError when finalize response indicates failure."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": False, "message": "timeout"}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        session = MagicMock()
        session.get.return_value = mock_resp
        with pytest.raises(RuntimeError, match="done failed"):
            finalize_upload(session, "https://api.test", "task1", api_key=None, timeout=300)


def _make_cfg(  # noqa: PLR0913
    *,
    base_url: str = "https://api.test",
    api_key: str | None = None,
    chunk_size: int = 8192,
    retries: int = 3,
    part_retries: int = 2,
    backoff: int = 2,
    timeout: int = 30,
    dry_run: bool = False,
    verbose: bool = False,
    progress: bool = False,
    session: niquests.Session | None = None,
    state: StateFile | None = None,
    chunk_concurrency: int = 1,
) -> UploadConfig:
    """Create an UploadConfig for testing."""
    return UploadConfig(
        base_url=base_url,
        api_key=api_key,
        chunk_size=chunk_size,
        retries=retries,
        part_retries=part_retries,
        backoff=backoff,
        timeout=timeout,
        dry_run=dry_run,
        verbose=verbose,
        progress=progress,
        session=session,
        state=state,
        chunk_concurrency=chunk_concurrency,
    )


class TestUploadOne:
    """Tests for the upload_one function."""

    def test_dry_run(self, tmp_path: Path) -> None:
        """Dry run returns a result without making API calls."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        cfg = _make_cfg(dry_run=True)
        result = upload_one(f, cfg)
        assert result["pillows_su_link"] == "https://pillows.su/f/DRY_RUN_test.mp3"
        assert "file_path" in result
        assert "size" in result

    @patch("pillows_upload.upload.finalize_upload")
    @patch("pillows_upload.upload.upload_part")
    @patch("pillows_upload.upload.init_upload")
    def test_single_chunk(
        self,
        mock_init: MagicMock,
        mock_part: MagicMock,
        mock_finalize: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A small file uploads as a single chunk."""
        f = tmp_path / "small.mp3"
        f.write_bytes(b"\x00" * 100)
        mock_init.return_value = "task1"
        mock_finalize.return_value = "file1"

        cfg = _make_cfg(api_key="key")
        result = upload_one(f, cfg)
        assert result["pillows_su_link"] == "https://pillows.su/f/file1"
        mock_init.assert_called_once()
        mock_part.assert_called_once()
        mock_finalize.assert_called_once()

    @patch("pillows_upload.upload.finalize_upload")
    @patch("pillows_upload.upload.upload_part")
    @patch("pillows_upload.upload.init_upload")
    def test_multi_chunk(
        self,
        mock_init: MagicMock,
        mock_part: MagicMock,
        mock_finalize: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A file larger than chunk_size is split into multiple chunks."""
        f = tmp_path / "big.mp3"
        f.write_bytes(b"\x00" * 100)
        mock_init.return_value = "task1"
        mock_finalize.return_value = "file1"

        cfg = _make_cfg(api_key="key", chunk_size=30)
        result = upload_one(f, cfg)
        assert result["pillows_su_link"] == "https://pillows.su/f/file1"
        assert mock_part.call_count == 4  # noqa: PLR2004

    @patch("pillows_upload.upload.time.sleep")
    @patch("pillows_upload.upload.finalize_upload")
    @patch("pillows_upload.upload.upload_part")
    @patch("pillows_upload.upload.init_upload")
    def test_retries_on_failure(
        self,
        mock_init: MagicMock,
        mock_part: MagicMock,  # noqa: ARG002
        mock_finalize: MagicMock,
        mock_sleep: MagicMock,  # noqa: ARG002
        tmp_path: Path,
    ) -> None:
        """Upload retries on transient failure and eventually succeeds."""
        f = tmp_path / "retry.mp3"
        f.write_bytes(b"\x00" * 10)
        mock_init.return_value = "task1"
        mock_finalize.side_effect = [RuntimeError("timeout"), "file1"]

        cfg = _make_cfg(api_key="key")
        result = upload_one(f, cfg)
        assert result["pillows_su_link"] == "https://pillows.su/f/file1"
        assert mock_finalize.call_count == 2  # noqa: PLR2004

    @patch("pillows_upload.upload.time.sleep")
    @patch("pillows_upload.upload.finalize_upload")
    @patch("pillows_upload.upload.upload_part")
    @patch("pillows_upload.upload.init_upload")
    def test_gives_up_after_max_retries(
        self,
        mock_init: MagicMock,
        mock_part: MagicMock,  # noqa: ARG002
        mock_finalize: MagicMock,
        mock_sleep: MagicMock,  # noqa: ARG002
        tmp_path: Path,
    ) -> None:
        """Upload raises RuntimeError after exhausting all retries."""
        f = tmp_path / "fail.mp3"
        f.write_bytes(b"\x00" * 10)
        mock_init.return_value = "task1"
        mock_finalize.side_effect = RuntimeError("always fails")

        cfg = _make_cfg(api_key="key")
        with pytest.raises(RuntimeError, match="Failed after 3 attempts"):
            upload_one(f, cfg)
        assert mock_finalize.call_count == 3  # noqa: PLR2004

    @patch("pillows_upload.upload.finalize_upload")
    @patch("pillows_upload.upload.upload_part")
    @patch("pillows_upload.upload.init_upload")
    def test_skips_unchanged_file(
        self,
        mock_init: MagicMock,
        mock_part: MagicMock,  # noqa: ARG002
        mock_finalize: MagicMock,  # noqa: ARG002
        tmp_path: Path,
    ) -> None:
        """Skips upload when file has not changed since last successful upload."""
        f = tmp_path / "song.mp3"
        f.write_bytes(b"\x00" * 100)
        sha = compute_sha256(f)
        state = StateFile(tmp_path / "state")
        state.record(str(f.resolve()), size=100, sha256=sha, parts_uploaded=4, url="https://pillows.su/f/abc")

        cfg = _make_cfg(api_key="key", state=state)
        result = upload_one(f, cfg)
        assert result["pillows_su_link"] == "https://pillows.su/f/abc"
        mock_init.assert_not_called()

    @patch("pillows_upload.upload.finalize_upload")
    @patch("pillows_upload.upload.upload_part")
    @patch("pillows_upload.upload.init_upload")
    def test_resumes_incomplete_upload(
        self,
        mock_init: MagicMock,
        mock_part: MagicMock,
        mock_finalize: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Resumes an incomplete upload from the last uploaded part."""
        f = tmp_path / "big.mp3"
        f.write_bytes(b"\x00" * 100)
        state = StateFile(tmp_path / "state")
        state.record(str(f.resolve()), size=100, sha256=compute_sha256(f), parts_uploaded=2, url="")

        mock_init.return_value = "task1"
        mock_finalize.return_value = "file1"

        cfg = _make_cfg(api_key="key", chunk_size=30, state=state)
        result = upload_one(f, cfg)
        assert result["pillows_su_link"] == "https://pillows.su/f/file1"
        assert mock_part.call_count == 2  # noqa: PLR2004

    @patch("pillows_upload.upload.finalize_upload")
    @patch("pillows_upload.upload.upload_part")
    @patch("pillows_upload.upload.init_upload")
    def test_chunk_concurrency_flag_accepted(
        self,
        mock_init: MagicMock,
        mock_part: MagicMock,
        mock_finalize: MagicMock,
        tmp_path: Path,
    ) -> None:
        """chunk_concurrency parameter is accepted and used (real executor)."""
        f = tmp_path / "big.mp3"
        f.write_bytes(b"\x00" * 100)
        mock_init.return_value = "task1"
        mock_finalize.return_value = "file1"

        cfg = _make_cfg(api_key="key", chunk_size=30, chunk_concurrency=2)
        result = upload_one(f, cfg)
        assert result["pillows_su_link"] == "https://pillows.su/f/file1"
        # 100 bytes / 30 byte chunks -> 4 parts uploaded concurrently
        assert mock_part.call_count == 4  # noqa: PLR2004

    @patch("pillows_upload.upload.finalize_upload")
    @patch("pillows_upload.upload.upload_part")
    @patch("pillows_upload.upload.init_upload")
    def test_upload_one_saves_state_on_success(
        self,
        mock_init: MagicMock,
        mock_part: MagicMock,  # noqa: ARG002
        mock_finalize: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Successful upload records state for future resume."""
        f = tmp_path / "song.mp3"
        f.write_bytes(b"\x00" * 100)
        mock_init.return_value = "task1"
        mock_finalize.return_value = "file1"
        state = StateFile(tmp_path / "state")

        cfg = _make_cfg(api_key="key", state=state)
        result = upload_one(f, cfg)
        assert result["pillows_su_link"] == "https://pillows.su/f/file1"
        entry = state.get(str(f.resolve()))
        assert entry["url"] == "https://pillows.su/f/file1"
        assert entry["size"] == 100  # noqa: PLR2004

    @patch("pillows_upload.upload.finalize_upload")
    @patch("pillows_upload.upload.upload_part")
    @patch("pillows_upload.upload.init_upload")
    def test_hash_mismatch_does_not_skip(
        self,
        mock_init: MagicMock,
        mock_part: MagicMock,  # noqa: ARG002
        mock_finalize: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Upload proceeds when cached hash does not match current file."""
        f = tmp_path / "song.mp3"
        f.write_bytes(b"\x00" * 100)
        state = StateFile(tmp_path / "state")
        state.record(str(f.resolve()), size=999, sha256="wrong", parts_uploaded=4, url="https://old.url")

        mock_init.return_value = "task1"
        mock_finalize.return_value = "file1"

        cfg = _make_cfg(api_key="key", state=state)
        result = upload_one(f, cfg)
        assert result["pillows_su_link"] == "https://pillows.su/f/file1"
        mock_init.assert_called_once()


class TestUploadTask:
    """Tests for the upload_task function."""

    def test_success(self, tmp_path: Path) -> None:
        """Dry-run upload_task returns a result dict."""
        f = tmp_path / "ok.mp3"
        f.write_bytes(b"\x00" * 10)
        args = argparse.Namespace(
            base_url="https://api.test",
            api_key=None,
            chunk_size=8192,
            retries=3,
            part_retries=2,
            backoff=2,
            dry_run=True,
            verbose=False,
            timeout=30,
            no_progress=False,
            chunk_concurrency=1,
        )
        result = upload_task(f, args, set())
        assert result is not None
        assert "pillows_su_link" in result
        assert "file_path" in result

    def test_failure_returns_none(self, tmp_path: Path) -> None:
        """upload_task returns None when upload fails."""
        f = tmp_path / "bad.mp3"
        f.write_bytes(b"\x00" * 10)
        args = argparse.Namespace(
            base_url="https://api.test",
            api_key=None,
            chunk_size=8192,
            retries=1,
            part_retries=2,
            backoff=2,
            dry_run=False,
            verbose=False,
            timeout=30,
            no_progress=False,
            chunk_concurrency=1,
        )
        with patch("pillows_upload.upload.init_upload", side_effect=RuntimeError("nope")):
            result = upload_task(f, args, set())
        assert result is None


class TestMain:
    """Tests for the main CLI entry point."""

    def test_no_files_returns_1(self, tmp_path: Path) -> None:
        """Returns exit code 1 when no files match."""
        result = main([str(tmp_path / "nonexistent")])
        assert result == 1

    def test_dry_run_writes_csv(self, tmp_path: Path) -> None:
        """Dry run with --no-csv does not create an output file."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        csv_out = tmp_path / "out.csv"
        result = main(
            [
                str(f),
                "--dry-run",
                "-o",
                str(csv_out),
                "--no-csv",
            ],
        )
        assert result == 0
        assert not csv_out.exists()

    def test_dry_run_with_csv(self, tmp_path: Path) -> None:
        """Dry run with CSV output creates a valid CSV file."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        csv_out = tmp_path / "out.csv"
        result = main(
            [
                str(f),
                "--dry-run",
                "-o",
                str(csv_out),
            ],
        )
        assert result == 0
        assert csv_out.exists()
        with csv_out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["file_path"] == str(f.resolve())

    def test_resume_skips_uploaded(self, tmp_path: Path) -> None:
        """Resume mode skips files already recorded in state."""
        f1 = tmp_path / "a.mp3"
        f2 = tmp_path / "b.mp3"
        f1.write_bytes(b"\x00" * 10)
        f2.write_bytes(b"\x00" * 10)
        state = tmp_path.parent / "test_state"
        state.write_text(str(f1.resolve()) + "\n")

        csv_out = tmp_path / "out.csv"
        result = main(
            [
                str(tmp_path),
                "--dry-run",
                "--resume",
                "--state-file",
                str(state),
                "-o",
                str(csv_out),
            ],
        )
        assert result == 0
        with csv_out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["file_path"] == str(f2.resolve())

    @patch("pillows_upload.cli.Path.unlink")
    def test_delete_removes_files(self, mock_unlink: MagicMock, tmp_path: Path) -> None:
        """Delete flag unlinks files after successful upload."""
        f = tmp_path / "del.mp3"
        f.write_bytes(b"\x00" * 10)
        csv_out = tmp_path / "out.csv"
        result = main(
            [
                str(f),
                "--dry-run",
                "--delete",
                "-o",
                str(csv_out),
            ],
        )
        assert result == 0
        mock_unlink.assert_called_once()

    def test_ext_filter(self, tmp_path: Path) -> None:
        """Extension filter limits which files are uploaded."""
        (tmp_path / "a.mp3").write_bytes(b"\x00" * 10)
        (tmp_path / "b.txt").write_text("hi")
        csv_out = tmp_path / "out.csv"
        result = main(
            [
                str(tmp_path),
                "--ext",
                ".mp3",
                "--dry-run",
                "-o",
                str(csv_out),
            ],
        )
        assert result == 0
        with csv_out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        assert len(rows) == 1

    def test_min_size_filter(self, tmp_path: Path) -> None:
        """Minimum size filter excludes small files."""
        (tmp_path / "small.txt").write_text("hi")
        (tmp_path / "big.txt").write_text("x" * 100)
        csv_out = tmp_path / "out.csv"
        result = main(
            [
                str(tmp_path),
                "--min-size",
                "50",
                "--dry-run",
                "-o",
                str(csv_out),
            ],
        )
        assert result == 0
        with csv_out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        assert len(rows) == 1

    def test_quiet_suppresses_output(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Quiet mode suppresses info-level log output."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        with caplog.at_level(logging.INFO, logger="pillows_upload"):
            main([str(f), "--dry-run", "--quiet"])
        assert "Found" not in caplog.text
        assert "Uploading" not in caplog.text

    def test_format_json(self, tmp_path: Path) -> None:
        """JSON format produces a valid JSON output file."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        json_out = tmp_path / "out.json"
        result = main(
            [
                str(f),
                "--dry-run",
                "--format",
                "json",
                "-o",
                str(json_out),
            ],
        )
        assert result == 0
        with json_out.open() as fh:
            data = json.load(fh)
        assert len(data) == 1
        assert "pillows_su_link" in data[0]

    def test_format_ndjson(self, tmp_path: Path) -> None:
        """NDJSON format produces one JSON object per line."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        ndjson_out = tmp_path / "out.ndjson"
        result = main(
            [
                str(f),
                "--dry-run",
                "--format",
                "ndjson",
                "-o",
                str(ndjson_out),
            ],
        )
        assert result == 0
        lines = ndjson_out.read_text().strip().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["file_path"] == str(f.resolve())

    def test_verbose_shows_timing(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Verbose mode logs timing information."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        with caplog.at_level(logging.INFO, logger="pillows_upload"):
            main([str(f), "--dry-run", "-v"])
        assert "OK" in caplog.text
        assert "MB/s" in caplog.text

    def test_version_flag(self) -> None:
        """--version flag causes SystemExit(0)."""
        with pytest.raises(SystemExit) as exc_info:
            main(["--version"])
        assert exc_info.value.code == 0

    def test_validate_args_rejects_bad_chunk_size(self) -> None:
        """validate_args raises ValueError for non-positive chunk_size."""
        args = argparse.Namespace(
            chunk_size=0,
            concurrency=None,
            chunk_concurrency=None,
            retries=None,
            part_retries=None,
            backoff=None,
            timeout=None,
            min_size=None,
            max_size=None,
        )
        with pytest.raises(ValueError, match="--chunk-size must be greater than 0"):
            validate_args(args)

    def test_validate_args_rejects_negative_concurrency(self) -> None:
        """validate_args raises ValueError for concurrency less than 1."""
        args = argparse.Namespace(
            chunk_size=None,
            concurrency=-1,
            chunk_concurrency=None,
            retries=None,
            part_retries=None,
            backoff=None,
            timeout=None,
            min_size=None,
            max_size=None,
        )
        with pytest.raises(ValueError, match="--concurrency must be at least 1"):
            validate_args(args)

    def test_validate_args_rejects_bad_timeout(self) -> None:
        """validate_args raises ValueError for non-positive timeout."""
        args = argparse.Namespace(
            chunk_size=None,
            concurrency=None,
            chunk_concurrency=None,
            retries=None,
            part_retries=None,
            backoff=None,
            timeout=-1,
            min_size=None,
            max_size=None,
        )
        with pytest.raises(ValueError, match="--timeout must be greater than 0"):
            validate_args(args)

    def test_validate_args_rejects_max_lt_min(self) -> None:
        """validate_args raises ValueError when max_size < min_size."""
        args = argparse.Namespace(
            chunk_size=None,
            concurrency=None,
            chunk_concurrency=None,
            retries=None,
            part_retries=None,
            backoff=None,
            timeout=None,
            min_size=100,
            max_size=50,
        )
        with pytest.raises(ValueError, match="--max-size must be greater than or equal to --min-size"):
            validate_args(args)

    def test_summary_shows_total_bytes_and_speed(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Summary logs total bytes and upload speed."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 1000)
        with caplog.at_level(logging.INFO, logger="pillows_upload"):
            main([str(f), "--dry-run"])
        assert "Uploaded: 1" in caplog.text
        assert "MB" in caplog.text
        assert "MB/s" in caplog.text

    def test_validation_error_returns_1(self, tmp_path: Path) -> None:
        """Invalid arguments cause main() to return exit code 1."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        result = main([str(f), "--chunk-size", "0"])
        assert result == 1

    def test_chunk_concurrency_flag(self, tmp_path: Path) -> None:
        """--chunk-concurrency flag is accepted."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        csv_out = tmp_path / "out.csv"
        result = main(
            [
                str(f),
                "--dry-run",
                "--chunk-concurrency",
                "2",
                "-o",
                str(csv_out),
            ],
        )
        assert result == 0
        assert csv_out.exists()

    def test_quiet_suppresses_delete_message(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Quiet mode suppresses the 'Deleted' log message."""
        f = tmp_path / "del.mp3"
        f.write_bytes(b"\x00" * 10)
        with caplog.at_level(logging.INFO, logger="pillows_upload"):
            main([str(f), "--dry-run", "--delete", "--quiet"])
        assert "Deleted" not in caplog.text

    def test_format_default_creates_csv(self, tmp_path: Path) -> None:
        """Default format creates a CSV output file."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        csv_out = tmp_path / "out.csv"
        main([str(f), "--dry-run", "-o", str(csv_out)])
        assert csv_out.exists()

    def test_format_json_creates_json(self, tmp_path: Path) -> None:
        """--format json creates a JSON output file."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        json_out = tmp_path / "out.json"
        main([str(f), "--dry-run", "--format", "json", "-o", str(json_out)])
        assert json_out.exists()

    def test_no_csv_skips_output(self, tmp_path: Path) -> None:
        """--no-csv prevents output file creation."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        csv_out = tmp_path / "out.csv"
        result = main([str(f), "--dry-run", "--no-csv", "-o", str(csv_out)])
        assert result == 0
        assert not csv_out.exists()

    def test_ext_normalization_uppercase(self, tmp_path: Path) -> None:
        """Extension filter is case-insensitive."""
        (tmp_path / "a.MP3").write_bytes(b"\x00" * 10)
        (tmp_path / "b.txt").write_text("hi")
        csv_out = tmp_path / "out.csv"
        result = main(
            [
                str(tmp_path),
                "--ext",
                "mp3",
                "--dry-run",
                "-o",
                str(csv_out),
            ],
        )
        assert result == 0
        with csv_out.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        assert len(rows) == 1
        assert "a.MP3" in rows[0]["file_path"]

    def test_quiet_with_delete_no_output(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Quiet mode with delete suppresses all non-error output."""
        f = tmp_path / "del.mp3"
        f.write_bytes(b"\x00" * 10)
        with caplog.at_level(logging.INFO, logger="pillows_upload"):
            main([str(f), "--dry-run", "--delete", "--quiet"])
        assert "Uploading" not in caplog.text
        assert "Deleted" not in caplog.text
        assert "ERROR" not in caplog.text

    def test_version_flag_exits_zero(self) -> None:
        """--version exits with code 0."""
        with pytest.raises(SystemExit) as exc_info:
            main(["--version"])
        assert exc_info.value.code == 0

    def test_part_retries_validates(self, tmp_path: Path) -> None:
        """Negative --part-retries causes exit code 1."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        result = main([str(f), "--part-retries", "-1"])
        assert result == 1

    def test_backoff_validates(self, tmp_path: Path) -> None:
        """Zero --backoff causes exit code 1."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        result = main([str(f), "--backoff", "0"])
        assert result == 1


class TestUploadFiles:
    """Tests for the upload_files library function."""

    def test_dry_run(self, tmp_path: Path) -> None:
        """Dry-run returns results without API calls."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        results = upload_files(
            [str(tmp_path)],
            dry_run=True,
            no_progress=True,
            output=str(tmp_path / "out.csv"),
        )
        assert len(results) == 1
        assert "pillows_su_link" in results[0]

    def test_no_files_returns_empty(self, tmp_path: Path) -> None:
        """No matching files returns an empty list."""
        results = upload_files([str(tmp_path / "nonexistent")], dry_run=True)
        assert results == []

    def test_requires_api_key_when_not_dry_run(self, tmp_path: Path) -> None:
        """Non-dry-run raises ValueError without an API key."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        with pytest.raises(ValueError, match="API key required"):
            upload_files([str(tmp_path)], dry_run=False)

    def test_skips_unchanged_with_resume(self, tmp_path: Path) -> None:
        """Resume mode skips files already uploaded."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        state_file = tmp_path.parent / ".upload_state"
        state = StateFile(state_file)
        state.record(str(f.resolve()), size=10, sha256=compute_sha256(f), url="https://pillows.su/f/abc")
        results = upload_files(
            [str(tmp_path)],
            dry_run=True,
            resume=True,
            state_file=str(state_file),
            no_progress=True,
        )
        assert len(results) == 0

    def test_output_writer_called(self, tmp_path: Path) -> None:
        """Output file is created when output path is provided."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        csv_out = tmp_path / "out.csv"
        results = upload_files(
            [str(tmp_path)],
            dry_run=True,
            no_progress=True,
            output=str(csv_out),
            output_format="csv",
        )
        assert len(results) == 1
        assert csv_out.exists()

    def test_delete_removes_files(self, tmp_path: Path) -> None:
        """Delete flag processes files without error."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        results = upload_files(
            [str(tmp_path)],
            dry_run=True,
            no_progress=True,
            delete=True,
        )
        assert len(results) == 1


class TestPrintCompletions:
    """Tests for the print_completions function."""

    def test_bash_output(self, caplog: pytest.LogCaptureFixture) -> None:
        """Bash completion script is logged."""
        with caplog.at_level(logging.INFO, logger="pillows_upload"):
            print_completions("bash")
        assert "COMPREPLY" in caplog.text
        assert "complete -F _pillows_upload pillows-upload" in caplog.text

    def test_zsh_output(self, caplog: pytest.LogCaptureFixture) -> None:
        """Zsh completion script is logged."""
        with caplog.at_level(logging.INFO, logger="pillows_upload"):
            print_completions("zsh")
        assert "#compdef" in caplog.text
        assert "_arguments" in caplog.text

    def test_fish_output(self, caplog: pytest.LogCaptureFixture) -> None:
        """Fish completion script is logged."""
        with caplog.at_level(logging.INFO, logger="pillows_upload"):
            print_completions("fish")
        assert "complete -c pillows-upload" in caplog.text

    def test_unknown_shell_raises(self) -> None:
        """Unknown shell raises ValueError."""
        with pytest.raises(ValueError, match="Unknown shell"):
            print_completions("powershell")


class TestConfigExtended:
    """Extended tests for the Config class."""

    def test_explicit_key_value_path(self, tmp_path: Path) -> None:
        """Config loads from an explicit KEY=VALUE file path."""
        config_file = tmp_path / "custom.env"
        config_file.write_text('api_key = "custom123"\n')
        config = Config(str(config_file))
        assert config.get("api_key") == "custom123"

    def test_toml_non_string_values(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config loads int/float/bool TOML values as strings."""
        toml_file = tmp_path / "config.toml"
        toml_file.write_text("[pillows-upload]\nport = 8080\nratio = 1.5\ndry_run = true\n")
        monkeypatch.setattr(Config, "USER_CONFIG_FILE", tmp_path / "nonexistent")
        config = Config(str(toml_file))
        assert config.get("port") == "8080"
        assert config.get("ratio") == "1.5"
        assert config.get("dry_run") == "True"

    def test_toml_ignores_non_scalar(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config ignores lists and dicts in TOML sections."""
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[pillows-upload]\napi_key = "ok"\npaths = ["/a", "/b"]\nnested = { x = 1 }\n')
        monkeypatch.setattr(Config, "USER_CONFIG_FILE", tmp_path / "nonexistent")
        config = Config(str(toml_file))
        assert config.get("api_key") == "ok"
        assert config.get("paths") is None
        assert config.get("nested") is None

    def test_env_file_empty_value(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config handles empty values in config files."""
        config_file = tmp_path / "config"
        config_file.write_text("EMPTY_KEY=\n")
        monkeypatch.setattr(Config, "USER_CONFIG_FILE", config_file)
        config = Config()
        assert config.get("EMPTY_KEY") == ""

    def test_env_file_no_equals(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config skips lines without an equals sign."""
        config_file = tmp_path / "config"
        config_file.write_text("INVALID_LINE\nKEY=value\n")
        monkeypatch.setattr(Config, "USER_CONFIG_FILE", config_file)
        config = Config()
        assert config.get("KEY") == "value"

    def test_default_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config.get returns the default for missing keys."""
        monkeypatch.setattr(Config, "USER_CONFIG_FILE", tmp_path / "nonexistent")
        config = Config()
        assert config.get("missing", "fallback") == "fallback"


class TestValidateArgsExtended:
    """Extended tests for the validate_args function."""

    def test_rejects_negative_chunk_concurrency(self) -> None:
        """Negative chunk_concurrency raises ValueError."""
        args = argparse.Namespace(
            chunk_size=None,
            concurrency=None,
            chunk_concurrency=-1,
            retries=None,
            part_retries=None,
            backoff=None,
            timeout=None,
            min_size=None,
            max_size=None,
        )
        with pytest.raises(ValueError, match="--chunk-concurrency must be at least 1"):
            validate_args(args)

    def test_rejects_negative_retries(self) -> None:
        """Negative retries raises ValueError."""
        args = argparse.Namespace(
            chunk_size=None,
            concurrency=None,
            chunk_concurrency=None,
            retries=-1,
            part_retries=None,
            backoff=None,
            timeout=None,
            min_size=None,
            max_size=None,
        )
        with pytest.raises(ValueError, match="--retries must be 0 or greater"):
            validate_args(args)

    def test_rejects_negative_part_retries(self) -> None:
        """Negative part_retries raises ValueError."""
        args = argparse.Namespace(
            chunk_size=None,
            concurrency=None,
            chunk_concurrency=None,
            retries=None,
            part_retries=-1,
            backoff=None,
            timeout=None,
            min_size=None,
            max_size=None,
        )
        with pytest.raises(ValueError, match="--part-retries must be 0 or greater"):
            validate_args(args)

    def test_rejects_backoff_less_than_1(self) -> None:
        """Backoff less than 1 raises ValueError."""
        args = argparse.Namespace(
            chunk_size=None,
            concurrency=None,
            chunk_concurrency=None,
            retries=None,
            part_retries=None,
            backoff=0,
            timeout=None,
            min_size=None,
            max_size=None,
        )
        with pytest.raises(ValueError, match="--backoff must be at least 1"):
            validate_args(args)

    def test_rejects_negative_min_size(self) -> None:
        """Negative min_size raises ValueError."""
        args = argparse.Namespace(
            chunk_size=None,
            concurrency=None,
            chunk_concurrency=None,
            retries=None,
            part_retries=None,
            backoff=None,
            timeout=None,
            min_size=-1,
            max_size=None,
        )
        with pytest.raises(ValueError, match="--min-size must be 0 or greater"):
            validate_args(args)

    def test_rejects_negative_max_size(self) -> None:
        """Negative max_size raises ValueError."""
        args = argparse.Namespace(
            chunk_size=None,
            concurrency=None,
            chunk_concurrency=None,
            retries=None,
            part_retries=None,
            backoff=None,
            timeout=None,
            min_size=None,
            max_size=-1,
        )
        with pytest.raises(ValueError, match="--max-size must be 0 or greater"):
            validate_args(args)


class TestMainExtended:
    """Extended tests for the main CLI entry point."""

    def test_max_size_filter(self, tmp_path: Path) -> None:
        """Maximum size filter excludes large files."""
        (tmp_path / "small.txt").write_text("hi")
        (tmp_path / "big.txt").write_text("x" * 100)
        csv_out = tmp_path / "out.csv"
        result = main([str(tmp_path), "--max-size", "50", "--dry-run", "-o", str(csv_out)])
        assert result == 0
        with csv_out.open() as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 1
        assert rows[0]["file_path"] == str((tmp_path / "small.txt").resolve())

    def test_json_format_with_output(self, tmp_path: Path) -> None:
        """JSON format with output path creates a valid file."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        json_out = tmp_path / "out.json"
        result = main([str(f), "--dry-run", "--format", "json", "-o", str(json_out)])
        assert result == 0
        assert json_out.exists()
        with json_out.open() as fh:
            data = json.load(fh)
        assert len(data) == 1

    def test_html_format(self, tmp_path: Path) -> None:
        """HTML format creates a file with a table."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        html_out = tmp_path / "out.html"
        result = main([str(f), "--dry-run", "--format", "html", "-o", str(html_out)])
        assert result == 0
        assert html_out.exists()
        content = html_out.read_text()
        assert "<table>" in content

    def test_no_api_key_no_dry_run_fails(self, tmp_path: Path) -> None:
        """Missing API key without dry-run returns exit code 1."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.delenv("PILLOWS_KEY", raising=False)
        result = main([str(f)])
        assert result == 1

    def test_state_file_persists_across_invocations(self, tmp_path: Path) -> None:
        """State file is preserved between successive upload invocations."""
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00" * 10)
        state_file = tmp_path.parent / ".upload_state"

        result1 = main(
            [str(f), "--dry-run", "--resume", "--state-file", str(state_file), "-o", str(tmp_path / "out.csv")],
        )
        assert result1 == 0

        result2 = main(
            [str(f), "--dry-run", "--resume", "--state-file", str(state_file), "-o", str(tmp_path / "out2.csv")],
        )
        assert result2 == 0
        with (tmp_path / "out2.csv").open() as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 1


class TestDownload:
    """Tests for the download subcommand helpers."""

    def test_resolve_clonr_url(self) -> None:
        """clonr.co URLs decode the filename and keep the original URL."""
        url = "https://clonr.co/My%20Track.mp3"
        filename, download_url = _resolve_download_url(url)
        assert filename == "My Track.mp3"
        assert download_url == url

    def test_resolve_generic_url(self) -> None:
        """Generic URLs use the basename without query string."""
        url = "https://example.com/path/file.zip?token=abc"
        filename, download_url = _resolve_download_url(url)
        assert filename == "file.zip"
        assert download_url == url

    def test_resolve_imgur_returns_name_and_cdn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """imgur.gg/f/ URLs fetch name and CDN url from the API."""
        fake = {
            "name": "song.mp3",
            "cdnUrl": "https://cdn.example.com/song.mp3",
        }
        monkeypatch.setattr(
            "pillows_upload.download._http_get_json",
            lambda *_a, **_k: fake,
        )
        filename, download_url = _resolve_download_url("https://imgur.gg/f/abc123")
        assert filename == "song.mp3"
        assert download_url == "https://cdn.example.com/song.mp3"

    def test_resolve_imgur_missing_fields_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """imgur.gg/f/ URLs without name/cdn raise RuntimeError."""
        monkeypatch.setattr(
            "pillows_upload.download._http_get_json",
            lambda *_a, **_k: {},
        )
        with pytest.raises(RuntimeError, match="Failed to parse file info"):
            _resolve_download_url("https://imgur.gg/f/abc123")

    def test_http_get_json_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_http_get_json retries on failure then succeeds."""
        calls = {"n": 0}

        class FakeResp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"ok": True}

        def flaky_get(*_a: object, **_k: object) -> FakeResp:
            calls["n"] += 1
            if calls["n"] < 2:  # noqa: PLR2004
                msg = "boom"
                raise niquests.RequestException(msg)
            return FakeResp()

        monkeypatch.setattr(niquests, "get", flaky_get)
        result = _http_get_json("https://x.test", retries=3, backoff=1, timeout=5)
        assert result == {"ok": True}
        assert calls["n"] == 2  # noqa: PLR2004

    def test_http_get_json_exhausts_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_http_get_json raises after exhausting retries."""
        msg = "boom"
        monkeypatch.setattr(
            niquests,
            "get",
            lambda *_a, **_k: (_ for _ in ()).throw(niquests.RequestException(msg)),
        )
        with pytest.raises(RuntimeError, match="Failed to fetch"):
            _http_get_json("https://x.test", retries=2, backoff=1, timeout=5)

    def test_download_file_writes_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """_download_file streams bytes to the destination path."""
        payload = b"hello world"

        class FakeResp:
            def raise_for_status(self) -> None:
                return None

            def iter_content(self, **_k: object) -> object:
                yield payload

        monkeypatch.setattr(niquests, "get", lambda *_a, **_k: FakeResp())
        dest = tmp_path / "out.bin"
        _download_file(str(dest), dest, retries=1, backoff=1)
        assert dest.read_bytes() == payload

    def test_download_file_retries_then_succeeds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """_download_file retries transient failures before succeeding."""
        payload = b"data"
        calls = {"n": 0}

        class FakeResp:
            def raise_for_status(self) -> None:
                return None

            def iter_content(self, **_k: object) -> object:
                yield payload

        def flaky_get(*_a: object, **_k: object) -> FakeResp:
            calls["n"] += 1
            if calls["n"] < 2:  # noqa: PLR2004
                msg = "boom"
                raise niquests.RequestException(msg)
            return FakeResp()

        monkeypatch.setattr(niquests, "get", flaky_get)
        dest = tmp_path / "out.bin"
        _download_file(str(dest), dest, retries=3, backoff=1)
        assert dest.read_bytes() == payload
        assert calls["n"] == 2  # noqa: PLR2004

    def test_cmd_download_runs_end_to_end(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_cmd_download resolves, downloads, uploads, and prints the link."""
        url = "https://clonr.co/My%20Track.mp3"

        class FakeResp:
            def raise_for_status(self) -> None:
                return None

            def iter_content(self, **_k: object) -> object:
                yield b"audio"

        monkeypatch.setattr(niquests, "get", lambda *_a, **_k: FakeResp())
        monkeypatch.setattr(
            "pillows_upload.download.upload_one",
            lambda *_a, **_k: {"pillows_su_link": "https://pillows.su/f/xyz"},
        )
        code = _cmd_download([url, "--dry-run", "-q"])
        assert code == 0
