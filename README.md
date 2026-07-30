# pillows-upload

Bulk upload files to [pillows.su](https://pillows.su) via the chunked upload API.

Current version: 0.1.0

## Shell Completions

Generate completions for your shell:

```bash
# Bash
eval "$(pillows-upload --completions bash)" >> ~/.bashrc

# Zsh
eval "$(pillows-upload --completions zsh)" >> ~/.zshrc

# Fish
pillows-upload --completions fish > ~/.config/fish/completions/pillows-upload.fish
```

## Install

```bash
pip install git+https://github.com/edideaur/pillows-upload.git
```

Or install manually with [uv](https://github.com/astral-sh/uv):

```bash
git clone https://github.com/edideaur/pillows-upload.git
cd pillows-upload
uv pip install -e .
```

## Usage

```bash
pillows-upload [OPTIONS] [PATHS...]
```

**PATHS** - files or directories to upload. Defaults to `./downloads`.

```bash
# Upload everything in ./downloads
pillows-upload

# Upload specific files
pillows-upload song.mp3 image.png

# Upload a directory
pillows-upload ~/Music
```

## Download & Upload

The `download` subcommand fetches a file from a URL and uploads it directly to pillows.su, then prints the final link:

```bash
pillows-upload download "https://clonr.co/My%20Track.mp3"
pillows-upload download "https://imgur.gg/f/abc123"
pillows-upload download "https://example.com/file.zip"
```

Supported URL types:
- **clonr.co** - URL-decodes the filename, downloads from the original URL
- **imgur.gg/f/\<id\>** - fetches name + CDN URL from the imgur.gg API, downloads from CDN
- **generic URLs** - strips query params and uses the basename

## Upload to imgur.gg

The `imgur-upload` subcommand uploads local files directly to [imgur.gg](https://imgur.gg) via its API. Files are registered in batches of up to 50, then uploaded and finalized concurrently:

```bash
pillows-upload imgur-upload ~/Pictures -k "$IMGUR_KEY"
pillows-upload imgur-upload song.mp3 cover.png -c 4 --dry-run
```

- Auth uses the `x-api-key` header. The key is read from `-k/--api-key`, then the `IMGUR_KEY` environment variable, then `imgur_api_key` in the config file.
- Large files are uploaded using imgur.gg's multipart flow automatically.
- Concurrency is controlled with `-c/--concurrency` (parallel files). For maximum throughput use `-T/--turbo`.
- Resume uploads across runs with `--resume` (uses `--state-file`, default `.imgur_upload_state`, kept separate from the pillows.su state file).

## Maximum throughput

Both the pillows.su upload and the imgur.gg upload leverage [niquests](https://github.com/jawah/niquests), which negotiates **HTTP/3 (QUIC)** by default and multiplexes concurrent requests over a single connection (`--turbo` enables `multiplexed` sessions). To saturate bandwidth:

```bash
# pillows.su
pillows-upload -T -c 8 --chunk-concurrency 8 ~/Music

# imgur.gg
pillows-upload imgur-upload -T ~/Pictures
```

`-T/--turbo` is a preset that sets file concurrency and chunk concurrency to 8.

## Options

| Flag | Description |
|------|-------------|
| `-o, --output` | Output path (only written when `--format` is set) |
| `-k, --api-key` | API key (default: `PILLOWS_KEY` env var; `imgur-upload` uses `IMGUR_KEY`) |
| `--base-url` | API base URL (default: `https://api.pillows.su`) |
| `--chunk-size` | Chunk size in bytes (default: `8388608`) |
| `-c, --concurrency` | Parallel file uploads (default: `1`) |
| `-T, --turbo` | Max-throughput preset: file + chunk concurrency = `8` |
| `--chunk-concurrency` | Parallel chunk uploads per file (default: `1`) |
| `-r, --retries` | Retry count per file (default: `3`) |
| `--part-retries` | Retry count per chunk (default: `2`) |
| `--backoff` | Exponential backoff base (default: `2`) |
| `--timeout` | HTTP timeout in seconds (default: `30`) |
| `--dry-run` | Simulate uploads without sending data |
| `-v, --verbose` | Print detailed output |
| `-q, --quiet` | Suppress all non-error output |
| `--version` | Show version and exit |
| `--resume` | Skip files already uploaded (uses state file) |
| `--state-file` | State file path for resume (default: `.upload_state`) |
| `--delete` | Delete local files after successful upload |
| `--no-progress` | Disable progress bars |
| `--completions SHELL` | Print shell completions (`bash`, `zsh`, or `fish`) |
| `--config PATH` | Config file path (default: `~/.config/pillows-upload/config`) |
| `--ext` | Only upload files with these extensions (e.g. `.mp3 .wav`) |
| `--min-size` | Skip files smaller than N bytes |
| `--max-size` | Skip files larger than N bytes (`0` = no limit) |
| `--format` | Output format: `csv`, `json`, `ndjson`, `html`, `xlsx` |
| `--json-log` | Emit structured JSON log lines instead of plain text |
| `--adaptive` | Auto-tune concurrency up on success, down on errors |
| `--no-circuit-breaker` | Disable the circuit breaker that pauses after repeated failures |

## Examples

```bash
# Upload only mp3 and wav files, verbose output
pillows-upload --ext .mp3 .wav -v

# Dry run to see what would be uploaded
pillows-upload --dry-run -v

# Upload with 4 parallel workers
pillows-upload -c 4

# Resume a previous upload session
pillows-upload --resume

# Disable progress bars
pillows-upload --no-progress

# Delete files after upload
pillows-upload --delete

# Skip files under 1MB
pillows-upload --min-size 1048576

# Use a custom API key
pillows-upload -k YOUR_API_KEY
```

## Config File

Set defaults in `~/.config/pillows-upload/config` (or pass `--config PATH`) so you don't have to repeat flags. Both `KEY=VALUE` and TOML formats are supported.

**`~/.config/pillows-upload/config`**
```bash
PILLOWS_KEY=your-key
BASE_URL=https://api.pillows.su
CHUNK_SIZE=8388608
```

**`config.toml`**
```toml
[pillows-upload]
api_key = "your-key"
base_url = "https://api.pillows.su"
chunk_size = "8388608"
```

Environment variables (`PILLOWS_KEY`) take priority over the config file. CLI flags always override both.

## Output Formats

Use `--format` to choose the output type:

```bash
pillows-upload --format ndjson
```

| Format | Description |
|--------|-------------|
| `csv` | Standard CSV with header (default) |
| `json` | JSON array written after all uploads complete |
| `ndjson` | JSON Lines - one result object per line, streamed as uploads finish |
| `html` | Simple HTML table |
| `xlsx` | Excel workbook (requires `openpyxl`) |

Output is written only when you pass `--format` (or `-o`). By default no output file is created.

## State File

The state file (default: `.upload_state`) tracks uploaded files using JSON Lines. Each entry stores the file path, size, SHA-256, parts uploaded, and final URL. This enables:

- **Resume** - re-run the same command and already-uploaded files are skipped
- **Hash cache** - unchanged files are skipped automatically
- **Partial resume** - if an upload is interrupted, remaining chunks resume from the last successful part (assumes the API handles idempotent/duplicate part uploads)

## Operational subcommands

Beyond uploading, `pillows-upload` ships several management subcommands:

```bash
# Read/write config without editing files
pillows-upload config set PILLOWS_KEY your-key
pillows-upload config get PILLOWS_KEY
pillows-upload config list

# Diagnose environment: keys, network reachability, negotiated HTTP version
pillows-upload doctor

# Throughput benchmark (generates dummy files, measures MB/s)
pillows-upload bench --count 16 --size 16777216 -c 8

# Watch a directory and upload new files as they appear
pillows-upload watch ./inbox --interval 10 --delete-after

# Best-effort remote file management (if the server exposes it)
pillows-upload ls
pillows-upload rm <file_id>

# Download & upload multiple URLs from the CLI or a file
pillows-upload download url1 url2 --list urls.txt
```

### Structured logging

Set `--json-log` (or the `PILLOWS_JSON_LOG=1` environment variable) to emit one
JSON object per log line (`ts`, `level`, `logger`, `msg`) instead of plain text.
This is handy for ingesting logs into a pipeline.

### HTTP/3 and resilience

- **HTTP/3 (QUIC)** is negotiated by default. To force it (and disable HTTP/1+2),
  set `PILLOWS_FORCE_HTTP3=1`. The negotiated protocol is logged once per host.
- **Circuit breaker** pauses uploads after 5 consecutive failures for 30s, then
  resumes — disable with `--no-circuit-breaker`.
- **Pre-flight key check** verifies the API key against the host before uploading
  (skipped on `--dry-run`); a rejected key fails fast with a clear message.
- **Adaptive concurrency** (`--adaptive`) ramps concurrency up on success and
  down on errors to self-tune throughput.

## Exit Codes

- `0` - all files uploaded successfully
- `1` - one or more uploads failed or no files found
- `130` - interrupted by user (Ctrl+C), progress saved to state file

## Input Validation

The CLI validates all numeric inputs before starting:

- `--chunk-size` must be > 0
- `--concurrency` must be >= 1
- `--chunk-concurrency` must be >= 1
- `--retries` must be >= 0
- `--part-retries` must be >= 0
- `--backoff` must be >= 1
- `--timeout` must be > 0
- `--min-size` must be >= 0
- `--max-size` must be 0 or >= --min-size

## Library Usage

You can import and use `upload_files` directly in your Python app:

```python
from pillows_upload import upload_files

results = upload_files(
    paths=["./downloads"],
    api_key="your-api-key",
    extensions=[".mp3", ".wav"],
    concurrency=4,
    output="results.json",
    output_format="json",
    resume=True,
    delete=False,
)

for r in results:
    print(r["pillows_su_link"])
```

Available imports:

- `upload_files` - high-level convenience function
- `upload_one` - upload a single Path
- `StateFile` - JSON Lines state persistence with resume support
- `Config` - config loader for `~/.config/pillows-upload/config`
- `OutputWriter` - streaming CSV, NDJSON, JSON, HTML, XLSX writer

## Getting an API key

1. Pay for pillows premium (10$/month, crypto only)
2. If you run or edit a music tracker, join the trackerhub music discord, then ask for pillows premium in their channel (you must have the editors role) or DM Fragger, otherwise e-mail their contact address and ask for one.
